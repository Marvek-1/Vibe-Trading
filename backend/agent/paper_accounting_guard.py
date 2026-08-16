"""Fail-closed valuation and accounting guards for paper trading.

This module is dependency-free so it can be unit-tested without importing
ccxt, the API server, or the mutable paper-session runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isclose, isfinite
from typing import Any, Literal, Mapping, Sequence


AccountingState = Literal["OK", "DEFERRED", "ERROR"]


class PriceSnapshotError(RuntimeError):
    """Raised before ledger mutation when a mark snapshot is incomplete."""

    def __init__(
        self,
        *,
        missing_symbols: Sequence[str] = (),
        invalid_symbols: Sequence[str] = (),
    ) -> None:
        self.missing_symbols = tuple(sorted(set(missing_symbols)))
        self.invalid_symbols = tuple(sorted(set(invalid_symbols)))
        details: list[str] = []
        if self.missing_symbols:
            details.append(f"missing={list(self.missing_symbols)}")
        if self.invalid_symbols:
            details.append(f"invalid={list(self.invalid_symbols)}")
        super().__init__("incomplete price snapshot: " + ", ".join(details))


@dataclass(frozen=True)
class AccountingDecision:
    state: AccountingState
    reason: str
    residual: float | None
    tolerance: float | None
    stale_mark_symbols: tuple[str, ...] = ()
    position_differences: dict[str, dict[str, float]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_price_snapshot(
    symbols: Sequence[str],
    prices: Mapping[str, Any],
) -> dict[str, float]:
    """Return finite, strictly positive prices for every configured symbol.

    Extra prices are ignored. Missing, non-numeric, NaN, infinite, zero, and
    negative values all fail before any position, cash, trade, or mark mutation.
    """
    missing: list[str] = []
    invalid: list[str] = []
    normalized: dict[str, float] = {}

    for symbol in symbols:
        if symbol not in prices:
            missing.append(symbol)
            continue
        value = prices[symbol]
        if isinstance(value, bool):
            invalid.append(symbol)
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            invalid.append(symbol)
            continue
        if not isfinite(numeric) or numeric <= 0.0:
            invalid.append(symbol)
            continue
        normalized[symbol] = numeric

    if missing or invalid:
        raise PriceSnapshotError(
            missing_symbols=missing,
            invalid_symbols=invalid,
        )
    return normalized


def position_ledger_differences(
    book_positions: Mapping[str, Any],
    by_symbol: Mapping[str, Mapping[str, Any]],
    *,
    abs_tolerance: float = 1e-12,
    rel_tolerance: float = 1e-9,
) -> dict[str, dict[str, float]]:
    """Compare mutable book quantities with quantities reconstructed from trades."""
    symbols = set(book_positions) | set(by_symbol)
    differences: dict[str, dict[str, float]] = {}

    for symbol in sorted(symbols):
        book_qty = float(book_positions.get(symbol, 0.0) or 0.0)
        trade_qty = float(by_symbol.get(symbol, {}).get("open_qty", 0.0) or 0.0)
        if not isclose(
            book_qty,
            trade_qty,
            abs_tol=abs_tolerance,
            rel_tol=rel_tolerance,
        ):
            differences[symbol] = {
                "book_qty": book_qty,
                "trade_qty": trade_qty,
                "difference": book_qty - trade_qty,
            }
    return differences


def assess_accounting(
    *,
    configured_symbols: Sequence[str],
    initial_cash: float,
    equity: float,
    realized_pnl: float,
    unrealized_pnl: float | None,
    stale_mark_symbols: Sequence[str] = (),
    position_differences: Mapping[str, Mapping[str, float]] | None = None,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 1e-9,
) -> AccountingDecision:
    """Classify an accounting check as valid, indeterminate, or corrupted.

    DEFERRED means the valuation evidence is incomplete. It must not freeze a
    session. ERROR is reserved for a fully-valued numerical violation or a
    deterministic ledger/configuration mismatch.
    """
    configured = set(configured_symbols)
    stale = tuple(sorted(set(stale_mark_symbols)))
    diffs = dict(position_differences or {})

    if diffs:
        return AccountingDecision(
            state="ERROR",
            reason="POSITION_LEDGER_MISMATCH",
            residual=None,
            tolerance=None,
            stale_mark_symbols=stale,
            position_differences=diffs,
        )

    unconfigured_stale = tuple(symbol for symbol in stale if symbol not in configured)
    if unconfigured_stale:
        return AccountingDecision(
            state="ERROR",
            reason="LEDGER_SYMBOL_MISMATCH",
            residual=None,
            tolerance=None,
            stale_mark_symbols=unconfigured_stale,
        )

    if stale:
        return AccountingDecision(
            state="DEFERRED",
            reason="INCOMPLETE_PRICE_SNAPSHOT",
            residual=None,
            tolerance=None,
            stale_mark_symbols=stale,
        )

    if unrealized_pnl is None:
        return AccountingDecision(
            state="DEFERRED",
            reason="UNAVAILABLE_UNREALIZED_PNL",
            residual=None,
            tolerance=None,
        )

    values = (initial_cash, equity, realized_pnl, unrealized_pnl)
    if not all(isfinite(float(value)) for value in values):
        return AccountingDecision(
            state="ERROR",
            reason="NON_FINITE_ACCOUNTING_VALUE",
            residual=None,
            tolerance=None,
        )

    residual = float(equity) - (
        float(initial_cash) + float(realized_pnl) + float(unrealized_pnl)
    )
    tolerance = max(float(abs_tolerance), abs(float(equity)) * float(rel_tolerance))

    if abs(residual) <= tolerance:
        return AccountingDecision(
            state="OK",
            reason="RECONCILED",
            residual=residual,
            tolerance=tolerance,
        )

    return AccountingDecision(
        state="ERROR",
        reason="SELF_FINANCING_RESIDUAL",
        residual=residual,
        tolerance=tolerance,
    )


def validate_trade_closing_attribution(
    annotated_trades: Sequence[Mapping[str, Any]],
    *,
    tolerance: float = 1e-9,
) -> list[dict[str, Any]]:
    """Independent check on paper_session.compute_trade_stats's annotations:
    a trade may only realize P&L against the quantity that actually opposed
    the pre-trade position.

    This exists because a balanced portfolio-level equity identity is not
    sufficient evidence that realized/unrealized P&L was split correctly --
    a SELL that opens a short from flat and a matching fabricated unrealized
    loss on that same phantom cost basis can cancel in the aggregate
    invariant while each individual number is wrong (see the funding_live
    BTC-USDT SELL that was originally booked as a $199.94 win when it was
    actually opening a short at cost). This recomputes the expected closing
    quantity from each trade's own side/qty and the position it was
    annotated with, and flags any mismatch -- independent of whichever
    formula compute_trade_stats itself used, since it only reads the
    annotation's own recorded inputs (position_before, side, qty), not its
    derived output.

    Returns a list of violation dicts (empty if every trade's closed_qty
    matches what its own position_before/side/qty imply). Trades missing
    ``position_before`` (i.e. produced by something other than the current
    signed-position-aware compute_trade_stats) are skipped, not flagged.
    """
    violations: list[dict[str, Any]] = []
    for index, trade in enumerate(annotated_trades):
        position_before = trade.get("position_before")
        if position_before is None:
            continue
        qty = float(trade["qty"])
        signed_qty = qty if trade["side"] == "BUY" else -qty
        opposes = position_before != 0 and (
            (position_before > 0 and signed_qty < 0) or (position_before < 0 and signed_qty > 0)
        )
        expected_closed_qty = min(abs(position_before), qty) if opposes else 0.0
        actual_closed_qty = float(trade.get("closed_qty", 0.0) or 0.0)
        if abs(actual_closed_qty - expected_closed_qty) > tolerance:
            violations.append({
                "index": index,
                "symbol": trade.get("symbol"),
                "timestamp": trade.get("timestamp"),
                "side": trade.get("side"),
                "position_before": position_before,
                "expected_closed_qty": expected_closed_qty,
                "actual_closed_qty": actual_closed_qty,
            })

        closed_qty = trade.get("closed_qty")
        realized_pnl = trade.get("realized_pnl")
        if closed_qty is not None and closed_qty <= tolerance and realized_pnl is not None:
            violations.append({
                "index": index,
                "symbol": trade.get("symbol"),
                "timestamp": trade.get("timestamp"),
                "reason": "realized_pnl set on a trade with no closing quantity",
                "closed_qty": closed_qty,
                "realized_pnl": realized_pnl,
            })
    return violations


def validate_inventory_reconciliation(
    by_symbol: Mapping[str, Mapping[str, Any]],
    book_positions: Mapping[str, Any],
    *,
    abs_tolerance: float = 1e-9,
    rel_tolerance: float = 1e-9,
) -> dict[str, dict[str, float]]:
    """Independent check that trade_stats' replayed signed position for
    every symbol (``open_qty``) matches book.json's actual signed quantity
    -- catches incorrect side handling (e.g. a short misread as closing a
    long) even when no accounting-invariant residual is visible, since a
    misclassified side can still leave cash/equity self-consistent while
    the *inventory* it implies is wrong.

    This is position_ledger_differences' comparison specialized to make the
    "is this replay's inventory right" question explicit and independently
    callable, rather than only reachable bundled inside assess_accounting.
    """
    return position_ledger_differences(
        book_positions,
        by_symbol,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
    )
