import logging

import pytest

from hindsight_api.extensions.tenant import AuthenticationError
from hindsight_api.models import RequestContext
from hindsight_homelab.tenant import MultiKeyTenantExtension

KEYMAP = "key-aaa:tenant_one,key-bbb:tenant_two"

# A realistic-looking live key used only to assert it never leaks into an
# exception message or a log line.
_SECRET_KEY = "sk-super-secret-live-key-do-not-log-me"


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
async def test_unknown_and_missing_key_raise_the_identical_message():
    # The security-relevant half of "not a key oracle": a caller must not be
    # able to tell "wrong key" apart from "no key" from the error text.
    e = ext()
    with pytest.raises(AuthenticationError) as unknown_exc:
        await e.authenticate(RequestContext(api_key="key-zzz"))
    with pytest.raises(AuthenticationError) as missing_exc:
        await e.authenticate(RequestContext(api_key=None))
    assert str(unknown_exc.value) == str(missing_exc.value)


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


def test_empty_key_is_rejected_at_construction():
    with pytest.raises(ValueError):
        MultiKeyTenantExtension({"keymap": ":tenant_one"})


def test_empty_schema_is_rejected_at_construction():
    with pytest.raises(ValueError):
        MultiKeyTenantExtension({"keymap": "key-aaa:"})


@pytest.mark.asyncio
async def test_whitespace_around_entries_and_separator_is_tolerated():
    e = MultiKeyTenantExtension({"keymap": "  key-aaa : tenant_one ,  key-bbb:tenant_two  "})
    assert (await e.authenticate(RequestContext(api_key="key-aaa"))).schema_name == "tenant_one"
    assert (await e.authenticate(RequestContext(api_key="key-bbb"))).schema_name == "tenant_two"


def test_duplicate_key_error_does_not_leak_key_material():
    # The duplicate-key ValueError text ends up in startup logs (directly,
    # and again wrapped by hindsight_api's extension loader) — it must
    # identify the problem by position/schema only, never by the live key.
    with pytest.raises(ValueError) as excinfo:
        MultiKeyTenantExtension({"keymap": f"{_SECRET_KEY}:tenant_one,{_SECRET_KEY}:tenant_two"})
    assert _SECRET_KEY not in str(excinfo.value)


def test_malformed_entry_error_does_not_leak_key_material():
    # A malformed entry (missing ':') is, by definition, the raw key text
    # (or a fragment of it) — it must not be echoed back in the error.
    with pytest.raises(ValueError) as excinfo:
        MultiKeyTenantExtension({"keymap": _SECRET_KEY})
    assert _SECRET_KEY not in str(excinfo.value)


def test_empty_key_or_schema_error_does_not_leak_key_material():
    with pytest.raises(ValueError) as excinfo:
        MultiKeyTenantExtension({"keymap": f"{_SECRET_KEY}:"})
    assert _SECRET_KEY not in str(excinfo.value)


def test_reserved_colon_in_key_is_rejected_and_not_leaked():
    # A key containing ':' would otherwise be silently truncated by
    # partition(":") into a shorter, unintended "key" (the schema absorbs
    # the rest). Reject it loudly instead, without echoing the key.
    with pytest.raises(ValueError) as excinfo:
        MultiKeyTenantExtension({"keymap": f"{_SECRET_KEY}:extra:tenant_one"})
    assert _SECRET_KEY not in str(excinfo.value)


def test_duplicate_schema_across_different_keys_logs_a_warning(caplog):
    # Not fatal (key rotation can legitimately share a schema), but this is
    # exactly the failure mode the extension exists to prevent, so it must
    # be visible in the logs.
    with caplog.at_level(logging.WARNING, logger="hindsight_homelab.tenant"):
        MultiKeyTenantExtension({"keymap": "key-aaa:shared_schema,key-bbb:shared_schema"})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "shared_schema" in warnings[0].message
    assert "2" in warnings[0].message


def test_no_duplicate_schema_warning_when_schemas_are_distinct(caplog):
    with caplog.at_level(logging.WARNING, logger="hindsight_homelab.tenant"):
        ext()
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_distinct_schemas_are_logged_at_info_unconditionally(caplog):
    # An unconditional summary so a typo'd schema name is visible in startup
    # logs even when there's no duplicate to warn about.
    with caplog.at_level(logging.INFO, logger="hindsight_homelab.tenant"):
        ext()
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "tenant_one" in infos[0].message
    assert "tenant_two" in infos[0].message


def test_mcp_auth_token_env_var_is_rejected_at_construction(monkeypatch):
    # HINDSIGHT_API_MCP_AUTH_TOKEN is read directly by hindsight_api.api.mcp
    # and, if set, bypasses TenantExtension.authenticate_mcp() entirely —
    # every MCP caller would silently fall back to the default schema. Fail
    # closed at startup instead of letting that happen invisibly.
    monkeypatch.setenv("HINDSIGHT_API_MCP_AUTH_TOKEN", "some-static-token")
    with pytest.raises(ValueError):
        ext()
