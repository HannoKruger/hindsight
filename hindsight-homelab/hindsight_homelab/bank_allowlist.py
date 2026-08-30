"""Operation validator that refuses to create banks outside a fixed allowlist.

Hindsight creates a memory bank on first use: `retain` (or even `recall`) against
a bank id that does not exist yields a new, empty bank rather than an error. That
is convenient for ad-hoc use and actively harmful for a fixed deployment, because
the bank id travels in the URL. A client left pointing at a renamed bank, or a
typo in a connector URL, silently gets its own empty memory instead of a failure —
memory simply stops accumulating where anyone is looking, with nothing logged.

This extension pins the set of bank ids that may be created. Reads and writes to
existing banks are untouched; only *creation* is gated, so the allowlist is a
guard against drift, not an access-control boundary. Tenant isolation remains the
job of the tenant extension's per-key schema mapping — a caller can still only
ever address its own schema, and this check runs inside that schema.

Configure with (loaded from HINDSIGHT_API_OPERATION_VALIDATOR_ALLOWED_BANKS):

    HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_homelab.bank_allowlist:BankAllowlistValidator
    HINDSIGHT_API_OPERATION_VALIDATOR_ALLOWED_BANKS=hanno,hakru
"""

from __future__ import annotations

import logging

from hindsight_api.extensions.operation_validator import (
    CreateBankContext,
    OperationValidatorExtension,
    RecallContext,
    ReflectContext,
    RetainContext,
    ValidationResult,
)

logger = logging.getLogger(__name__)

_ALLOWED_BANKS_CONFIG_KEY = "allowed_banks"


def _parse_allowed_banks(raw: str) -> frozenset[str]:
    """Parse a comma-separated bank-id allowlist.

    Whitespace around entries is tolerated and empty entries are dropped, so a
    trailing comma is not an error. An allowlist that ends up empty is rejected:
    loading this extension with nothing to allow would silently permit every
    bank, which is the exact failure it exists to prevent.
    """
    banks = frozenset(entry.strip() for entry in raw.split(",") if entry.strip())
    if not banks:
        raise ValueError(
            "HINDSIGHT_API_OPERATION_VALIDATOR_ALLOWED_BANKS is empty. "
            "BankAllowlistValidator would then allow every bank to be created, "
            "which is the opposite of why it is loaded. Set it to a "
            "comma-separated list of bank ids (e.g. 'hanno,hakru'), or unset "
            "HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION to disable the check."
        )
    return banks


class BankAllowlistValidator(OperationValidatorExtension):
    """Rejects creation of any bank whose id is not in the configured allowlist."""

    def __init__(self, config: dict[str, str]):
        super().__init__(config)
        self._allowed = _parse_allowed_banks(config.get(_ALLOWED_BANKS_CONFIG_KEY, ""))
        logger.info(
            "BankAllowlistValidator: bank creation restricted to %d bank(s): %s",
            len(self._allowed),
            ", ".join(sorted(self._allowed)),
        )

    async def validate_create_bank(self, ctx: CreateBankContext) -> ValidationResult:
        if ctx.bank_id in self._allowed:
            return ValidationResult.accept()
        # The allowed ids are listed in the reason on purpose: the overwhelmingly
        # likely cause is a stale or mistyped URL, and naming the valid targets
        # turns a silent empty bank into a self-explaining error. Bank ids are
        # not secrets — the caller is already authenticated to this schema.
        logger.warning(
            "BankAllowlistValidator: refused creation of bank %r (allowed: %s)",
            ctx.bank_id,
            ", ".join(sorted(self._allowed)),
        )
        return ValidationResult.reject(
            f"Bank '{ctx.bank_id}' does not exist and may not be created. "
            f"Allowed banks: {', '.join(sorted(self._allowed))}. "
            "This usually means a client is pointing at an old or mistyped bank URL.",
            status_code=403,
        )

    # Core operations are not gated by this extension; it exists solely to stop
    # accidental bank creation. Accepting here keeps retain/recall/reflect on
    # existing banks exactly as they behave without the extension loaded.
    async def validate_retain(self, ctx: RetainContext) -> ValidationResult:
        return ValidationResult.accept()

    async def validate_recall(self, ctx: RecallContext) -> ValidationResult:
        return ValidationResult.accept()

    async def validate_reflect(self, ctx: ReflectContext) -> ValidationResult:
        return ValidationResult.accept()
