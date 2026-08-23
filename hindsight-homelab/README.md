# hindsight-homelab

Homelab extensions for [Hindsight](https://github.com/vectorize-io/hindsight).

## `MultiKeyTenantExtension`

A `TenantExtension` that maps a small, static set of API keys to per-tenant
PostgreSQL schemas. Unlike the builtin `ApiKeyTenantExtension` (which validates
one shared key and returns one schema for every caller), this extension gives
each key its own schema, so tenants' memories are isolated from each other at
the database level.

### Configuration

```
HINDSIGHT_API_TENANT_EXTENSION=hindsight_homelab.tenant:MultiKeyTenantExtension
HINDSIGHT_API_TENANT_KEYMAP=<key>:<schema>,<key>:<schema>
```

`HINDSIGHT_API_TENANT_KEYMAP` is a comma-separated list of `key:schema` pairs.
Whitespace around entries and around the `key:schema` separator is tolerated.
Each key must be unique — a duplicate key would silently merge two tenants
into one schema, so it is rejected at startup instead.

Example, mapping two callers to two isolated schemas:

```
HINDSIGHT_API_TENANT_KEYMAP=key-aaa:tenant_one,key-bbb:tenant_two
```

### `HINDSIGHT_API_MCP_AUTH_TOKEN` MUST be unset

This is a separate, legacy environment variable read directly by
`hindsight_api.api.mcp`, not by this extension. If it is set, the MCP
transport validates the caller's bearer token against it *before* any
`TenantExtension` is consulted, marks the request pre-authenticated, and
**never calls `authenticate_mcp()`**. No tenant schema gets resolved, so
every MCP caller falls back to the default (`public`) schema and shares one
memory space — silently defeating the entire point of this extension.

`MultiKeyTenantExtension` fails closed on this: the constructor raises
`ValueError` at startup if `HINDSIGHT_API_MCP_AUTH_TOKEN` is set, so a
deployment that misconfigures this refuses to start instead of quietly
merging every tenant.

### Behavior

- `authenticate()` resolves the caller's API key through the keymap. An
  unknown or missing key raises `AuthenticationError` with the same message
  in both cases, so a failed request cannot be used to distinguish "wrong
  key" from "no key" (i.e. the endpoint is not a key oracle).
- `authenticate_mcp()` delegates to `authenticate()` — MCP requests are
  authenticated exactly like HTTP requests, with no bypass *on this class*.
  As covered above, `HINDSIGHT_API_MCP_AUTH_TOKEN` is a bypass one layer up,
  in `hindsight_api.api.mcp` itself, which is why the constructor refuses to
  start when it's set — that's the only way to guarantee this method is
  actually reached for every MCP request.
- `list_tenants()` returns one `Tenant` per distinct schema in the keymap, so
  background workers poll every tenant's schema.
- All keymap validation (empty keymap, malformed entries, empty key/schema,
  duplicate keys) happens at construction time, so a bad configuration fails
  the deployment at startup rather than on the first request.

### Installing

This package has no runtime dependencies of its own — it only imports from
`hindsight_api`, which must already be installed in the same environment.
Install it with `--no-deps` so pip does not try to resolve `hindsight_api` a
second time:

```
pip install --no-deps ./hindsight-homelab
```
