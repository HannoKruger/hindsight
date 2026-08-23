"""Multi-tenant static-key tenant extension for Hindsight.

Hindsight's builtin `ApiKeyTenantExtension` authenticates one shared API key
and returns one schema for every caller — it authenticates callers but does
not isolate them. This extension maps a small, static set of API keys to
per-tenant PostgreSQL schemas, so each key gets its own isolated memory
space. It is meant for small, fixed-membership deployments (a handful of
tenants known ahead of time) where running a full tenant-provisioning system
would be overkill. See README.md for configuration.
"""

from __future__ import annotations

import os

from hindsight_api.extensions.tenant import AuthenticationError, Tenant, TenantContext, TenantExtension
from hindsight_api.models import RequestContext

# Unknown and missing keys raise with this exact same message so a failed
# request cannot be used to distinguish "wrong key" from "no key" — the
# endpoint must not become a key oracle.
_AUTH_FAILURE_MESSAGE = "Invalid API key"

# hindsight_api.api.mcp reads this var itself, before the tenant extension is
# ever consulted: if it is set, mcp.py validates the MCP bearer token against
# it directly, marks the request pre-authenticated, and never calls
# authenticate_mcp() at all. No schema is resolved, so every MCP caller falls
# back to the default ("public") schema and this extension's whole purpose —
# per-tenant isolation — is silently void, with no error anywhere. Refuse to
# start rather than let that happen invisibly.
_MCP_AUTH_TOKEN_ENV_VAR = "HINDSIGHT_API_MCP_AUTH_TOKEN"


def _parse_keymap(raw: str) -> dict[str, str]:
    """Parse a `key:schema,key:schema` string into a validated key->schema map.

    Whitespace around entries and around the `key:schema` separator is
    tolerated. Validation happens here, at construction time, so a bad
    HINDSIGHT_API_TENANT_KEYMAP fails the deployment at startup instead of on
    the first request.

    Raises:
        ValueError: If the keymap is empty, an entry is malformed (missing
            ':', or has an empty key or schema), or a key is duplicated.
    """
    keymap: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue

        if ":" not in entry:
            raise ValueError(f"Malformed HINDSIGHT_API_TENANT_KEYMAP entry (expected 'key:schema'): {entry!r}")

        key, _, schema = entry.partition(":")
        key = key.strip()
        schema = schema.strip()
        if not key or not schema:
            raise ValueError(f"Malformed HINDSIGHT_API_TENANT_KEYMAP entry (empty key or schema): {entry!r}")

        if key in keymap:
            raise ValueError(f"Duplicate key in HINDSIGHT_API_TENANT_KEYMAP: {key!r}")

        keymap[key] = schema

    if not keymap:
        raise ValueError("HINDSIGHT_API_TENANT_KEYMAP must contain at least one 'key:schema' entry")

    return keymap


class MultiKeyTenantExtension(TenantExtension):
    """Static-key tenant extension: one API key per tenant PostgreSQL schema.

    Configuration:
        HINDSIGHT_API_TENANT_EXTENSION=hindsight_homelab.tenant:MultiKeyTenantExtension
        HINDSIGHT_API_TENANT_KEYMAP=<key>:<schema>,<key>:<schema>

    Isolation comes from the schema returned by authenticate() — every query
    for a tenant is scoped to its own schema, so tenants never share a
    memory space. RequestContext.allowed_bank_ids is not enforced anywhere in
    the codebase, so it is not a substitute for schema isolation.

    HINDSIGHT_API_MCP_AUTH_TOKEN must be unset. That variable is a legacy,
    extension-independent MCP auth path in hindsight_api.api.mcp: when set,
    it bypasses TenantExtension.authenticate_mcp() entirely, so this
    extension is never consulted and every MCP caller collapses onto the
    default schema. The constructor below fails closed on it.
    """

    def __init__(self, config: dict[str, str]):
        super().__init__(config)
        if os.environ.get(_MCP_AUTH_TOKEN_ENV_VAR):
            raise ValueError(
                f"{_MCP_AUTH_TOKEN_ENV_VAR} is set. That variable makes the MCP transport "
                "bypass TenantExtension.authenticate_mcp() entirely (see hindsight_api.api.mcp), "
                "so MultiKeyTenantExtension is never consulted for MCP requests and every MCP "
                "caller silently falls back to the default schema — the per-tenant isolation "
                f"this extension exists to provide. Unset {_MCP_AUTH_TOKEN_ENV_VAR} to use "
                "MultiKeyTenantExtension."
            )
        self._keymap = _parse_keymap(config.get("keymap", ""))

    async def authenticate(self, context: RequestContext) -> TenantContext:
        """Resolve the caller's API key to its tenant schema.

        Raises:
            AuthenticationError: If the key is missing or not in the keymap.
                Both cases raise with the same message (see
                _AUTH_FAILURE_MESSAGE) so a caller cannot tell an unknown key
                apart from a missing one.
        """
        schema = self._keymap.get(context.api_key) if context.api_key else None
        if schema is None:
            raise AuthenticationError(_AUTH_FAILURE_MESSAGE)
        return TenantContext(schema_name=schema)

    async def authenticate_mcp(self, context: RequestContext) -> TenantContext:
        """Authenticate MCP requests exactly like HTTP requests.

        No bypass flag on this class: the builtin ApiKeyTenantExtension has a
        mcp_auth_disabled escape hatch, and this extension deliberately does
        not offer one. The one bypass that can still happen is external to
        this class entirely — HINDSIGHT_API_MCP_AUTH_TOKEN short-circuits
        hindsight_api.api.mcp before authenticate_mcp() is ever called — and
        __init__ refuses to start if that var is set, so reaching this method
        at all means no bypass is active.
        """
        return await self.authenticate(context)

    async def list_tenants(self) -> list[Tenant]:
        """Return one Tenant per distinct schema in the keymap.

        Background workers poll per schema; a schema missing from this list
        means that tenant's background tasks (e.g. consolidation) never run.
        """
        # dict.fromkeys preserves first-seen order while dropping duplicate
        # schemas (two keys could legitimately map to the same schema).
        return [Tenant(schema=schema, tenant_id=schema) for schema in dict.fromkeys(self._keymap.values())]
