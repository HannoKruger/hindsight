import logging

import pytest

from hindsight_api.extensions.operation_validator import CreateBankContext
from hindsight_api.models import RequestContext
from hindsight_homelab.bank_allowlist import BankAllowlistValidator

ALLOWED = "hanno,hakru"


def ext(allowed: str = ALLOWED):
    return BankAllowlistValidator({"allowed_banks": allowed})


def ctx(bank_id: str) -> CreateBankContext:
    return CreateBankContext(bank_id=bank_id, request_context=RequestContext(api_key="k"))


@pytest.mark.asyncio
@pytest.mark.parametrize("bank", ["hanno", "hakru"])
async def test_allowed_banks_may_be_created(bank):
    assert (await ext().validate_create_bank(ctx(bank))).allowed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("bank", ["hermes-hanno", "claude-code-hanno", "hermes-hakru"])
async def test_old_bank_ids_are_refused(bank):
    """A client left on a pre-rename URL must fail loudly, not get an empty bank."""
    result = await ext().validate_create_bank(ctx(bank))
    assert result.allowed is False
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_typo_and_empty_bank_ids_are_refused():
    for bank in ("hann", "HANNO", "hanno ", "", "random-junk"):
        assert (await ext().validate_create_bank(ctx(bank))).allowed is False


@pytest.mark.asyncio
async def test_rejection_names_the_valid_targets():
    """The reason must be self-explaining — a stale URL is the likely cause."""
    reason = (await ext().validate_create_bank(ctx("hermes-hanno"))).reason
    assert "hermes-hanno" in reason
    assert "hanno" in reason and "hakru" in reason


@pytest.mark.asyncio
async def test_core_operations_are_not_gated():
    e = ext()
    for hook in (e.validate_retain, e.validate_recall, e.validate_reflect):
        assert (await hook(None)).allowed is True


def test_whitespace_and_trailing_commas_tolerated():
    e = ext(" hanno , hakru ,")
    assert e._allowed == frozenset({"hanno", "hakru"})


@pytest.mark.parametrize("raw", ["", "   ", ",", " , ,"])
def test_empty_allowlist_refuses_to_load(raw):
    """Loading with nothing to allow would permit everything — fail at startup."""
    with pytest.raises(ValueError, match="ALLOWED_BANKS"):
        ext(raw)


def test_missing_config_key_refuses_to_load():
    with pytest.raises(ValueError):
        BankAllowlistValidator({})


def test_startup_logs_the_allowlist(caplog):
    with caplog.at_level(logging.INFO):
        ext()
    assert "hakru" in caplog.text and "hanno" in caplog.text


@pytest.mark.asyncio
async def test_refusal_is_logged(caplog):
    e = ext()
    with caplog.at_level(logging.WARNING):
        await e.validate_create_bank(ctx("hermes-hanno"))
    assert "hermes-hanno" in caplog.text
