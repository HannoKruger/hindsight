import pytest

from hindsight_api.extensions.tenant import AuthenticationError
from hindsight_api.models import RequestContext
from hindsight_homelab.tenant import MultiKeyTenantExtension

KEYMAP = "key-aaa:tenant_one,key-bbb:tenant_two"


def ext():
    return MultiKeyTenantExtension({"keymap": KEYMAP})


@pytest.mark.asyncio
async def test_known_key_maps_to_its_own_schema():
    e = ext()
    assert (await e.authenticate(RequestContext(api_key="key-aaa"))).schema_name == "tenant_one"
    assert (await e.authenticate(RequestContext(api_key="key-bbb"))).schema_name == "tenant_two"


@pytest.mark.asyncio
async def test_unknown_key_is_rejected():
    with pytest.raises(AuthenticationError):
        await ext().authenticate(RequestContext(api_key="key-zzz"))


@pytest.mark.asyncio
async def test_missing_key_is_rejected():
    with pytest.raises(AuthenticationError):
        await ext().authenticate(RequestContext(api_key=None))


@pytest.mark.asyncio
async def test_mcp_requests_are_authenticated_too():
    # MCP is the transport the agent uses; it must not bypass auth.
    e = ext()
    assert (await e.authenticate_mcp(RequestContext(api_key="key-aaa"))).schema_name == "tenant_one"
    with pytest.raises(AuthenticationError):
        await e.authenticate_mcp(RequestContext(api_key="key-zzz"))


@pytest.mark.asyncio
async def test_list_tenants_returns_every_schema_so_workers_poll_both():
    schemas = {t.schema for t in await ext().list_tenants()}
    assert schemas == {"tenant_one", "tenant_two"}


def test_empty_keymap_is_rejected_at_construction():
    with pytest.raises(ValueError):
        MultiKeyTenantExtension({"keymap": ""})


def test_malformed_keymap_entry_is_rejected_at_construction():
    with pytest.raises(ValueError):
        MultiKeyTenantExtension({"keymap": "key-aaa"})


def test_duplicate_key_is_rejected_at_construction():
    # Two schemas behind one key would silently merge two tenants.
    with pytest.raises(ValueError):
        MultiKeyTenantExtension({"keymap": "key-aaa:tenant_one,key-aaa:tenant_two"})


def test_mcp_auth_token_env_var_is_rejected_at_construction(monkeypatch):
    # HINDSIGHT_API_MCP_AUTH_TOKEN is read directly by hindsight_api.api.mcp
    # and, if set, bypasses TenantExtension.authenticate_mcp() entirely —
    # every MCP caller would silently fall back to the default schema. Fail
    # closed at startup instead of letting that happen invisibly.
    monkeypatch.setenv("HINDSIGHT_API_MCP_AUTH_TOKEN", "some-static-token")
    with pytest.raises(ValueError):
        ext()
