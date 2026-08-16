"""Pure reconciliation rules for account-scoped, per-leg positions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    account_id: str
    upserts: tuple[dict[str, Any], ...]
    deletes: tuple[str, ...]


def reconcile_positions(
    account_id: str,
    engine_open_positions: list[dict[str, Any]],
    stored_trade_ids: set[str],
) -> ReconciliationPlan:
    """Return per-leg writes and deletions for one authoritative snapshot.

    Validation happens before a database cursor is opened by the caller so a
    malformed snapshot cannot partially mutate storage.
    """
    engine_ids: set[str] = set()
    upserts: list[dict[str, Any]] = []
    for position in engine_open_positions:
        raw_trade_id = position.get("trade_id")
        if raw_trade_id is None or not str(raw_trade_id).strip():
            raise ValueError(
                f"engine position missing trade_id for account {account_id}: {position}"
            )
        trade_id = str(raw_trade_id)
        if trade_id in engine_ids:
            raise ValueError(
                f"duplicate trade_id {trade_id!r} within a single engine snapshot"
            )
        engine_ids.add(trade_id)
        upserts.append(position)

    return ReconciliationPlan(
        account_id=account_id,
        upserts=tuple(upserts),
        deletes=tuple(sorted(stored_trade_ids - engine_ids)),
    )


def validate_position_snapshot(
    account_id: str, engine_open_positions: list[dict[str, Any]]
) -> None:
    """Fail before SQL for missing or duplicate logical leg identifiers."""
    reconcile_positions(account_id, engine_open_positions, set())
