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

from hindsight_api.extensions.tenant import AuthenticationError, Tenant, TenantContext, TenantExtension
from hindsight_api.models import RequestContext

# Unknown and missing keys raise with this exact same message so a failed
# request cannot be used to distinguish "wrong key" from "no key" — the
# endpoint must not become a key oracle.
_AUTH_FAILURE_MESSAGE = "Invalid API key"


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
    """

    def __init__(self, config: dict[str, str]):
        super().__init__(config)
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

        No bypass flag: the builtin ApiKeyTenantExtension has a
        mcp_auth_disabled escape hatch, and this extension deliberately does
        not offer one — MCP is the transport the agent uses, and it must not
        skip auth.
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
