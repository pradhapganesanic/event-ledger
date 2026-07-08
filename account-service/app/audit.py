"""Audit trail via a dedicated `audit` logger (log-based — no DB table).

Audit records are emitted on the `audit` logger, so they form a SEPARATE,
filterable stream from the operational logs — grep on `"logger":"audit"`. They
reuse the same structured JSON formatter, so each audit line also carries the
timestamp, service name, and propagated `traceId`.

This is where the Account Service records the actual ledger action (TXN_APPLIED).
"""
import logging

_audit = logging.getLogger("audit")


def audit(action: str, outcome: str, **fields) -> None:
    """Emit one structured audit record (action, outcome, + context fields)."""
    _audit.info(
        action,
        extra={"extra_fields": {"audit": True, "action": action, "outcome": outcome, **fields}},
    )
