#!/usr/bin/env python3
"""Independent arithmetic check on replay_funding_session.py's output.

Deliberately does NOT import open_position, close_position, settle_funding,
or anything else from futures_ledger -- it re-derives the same 22-trade
journal's wallet/margin/P&L trajectory from a from-scratch implementation
using fractions.Fraction (exact rational arithmetic, a different numeric
type than the production kernel's Decimal) so a bug shared between "the
formula" and "the check" can't hide. Reads the raw trades.jsonl directly,
never the primary replay's report.json, for the event-by-event numbers --
report.json is only consulted at the end to compare final totals.

Usage:
    python -m accounting.verify_replay_independent \\
        --source paper_sessions/_quarantine/funding_live_accounting_invalid_<ts> \\
        --initial-capital 10000 \\
        --primary-report funding_live_reconciliation.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

LEVERAGE_ASSUMPTION = Fraction(2)
FEE_RATE = Fraction(5, 10000)  # 0.0005


@dataclass
class IndependentPosition:
    side: str  # "long" or "short"
    qty: Fraction
    entry_price: Fraction
    margin: Fraction
    entry_fee: Fraction


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _frac(value: Any) -> Fraction:
    # str(value) first -- never Fraction(float) directly, which would adopt
    # the float's exact binary value (denominator a power of two) instead
    # of the intended decimal quantity.
    return Fraction(str(value))


def independent_open(cash: Fraction, reserved: Fraction, side: str, qty: Fraction, price: Fraction) -> tuple[Fraction, Fraction, IndependentPosition, Fraction]:
    notional = qty * price
    margin = notional / LEVERAGE_ASSUMPTION
    fee = notional * FEE_RATE
    required = margin + fee
    if cash < required:
        raise ValueError(f"insufficient cash: need {required}, have {cash}")
    new_cash = cash - required
    new_reserved = reserved + margin
    position = IndependentPosition(side=side, qty=qty, entry_price=price, margin=margin, entry_fee=fee)
    return new_cash, new_reserved, position, fee


def independent_close(cash: Fraction, reserved: Fraction, position: IndependentPosition, price: Fraction) -> tuple[Fraction, Fraction, Fraction, Fraction]:
    sign = Fraction(1) if position.side == "long" else Fraction(-1)
    gross_pnl = sign * position.qty * (price - position.entry_price)
    exit_notional = position.qty * price
    exit_fee = exit_notional * FEE_RATE
    credit = position.margin + gross_pnl - exit_fee
    new_cash = cash + credit
    new_reserved = reserved - position.margin
    net_pnl = gross_pnl - exit_fee
    return new_cash, new_reserved, net_pnl, exit_fee


def verify(source_dir: Path, initial_capital: str) -> dict[str, Any]:
    trades = _read_jsonl(source_dir / "trades.jsonl")
    marks = _read_jsonl(source_dir / "marks.jsonl")

    cash = _frac(initial_capital)
    reserved = Fraction(0)
    open_positions: dict[str, IndependentPosition] = {}
    total_entry_fees = Fraction(0)
    total_exit_fees = Fraction(0)
    total_realized_pnl = Fraction(0)
    per_event_ok = []

    for i, t in enumerate(trades):
        symbol = t["symbol"]
        side_str = t["side"]
        qty = _frac(t["qty"])
        price = _frac(t["price"])
        current = open_positions.get(symbol)
        wallet_before = cash + reserved

        if current is None:
            side = "long" if side_str == "BUY" else "short"
            cash, reserved, position, fee = independent_open(cash, reserved, side, qty, price)
            open_positions[symbol] = position
            total_entry_fees += fee
            expected_delta = -fee
        else:
            cash, reserved, net_pnl, fee = independent_close(cash, reserved, current, price)
            del open_positions[symbol]
            total_exit_fees += fee
            # net_pnl (gross_pnl - exit_fee) is the correct wallet-conservation
            # figure for THIS event (entry_fee already hit the wallet at open
            # time). The realized_pnl summary statistic additionally nets the
            # position's own entry_fee, matching compute_trade_stats's
            # convention -- still-open positions' entry fees stay unrealized.
            total_realized_pnl += net_pnl - current.entry_fee
            expected_delta = net_pnl

        wallet_after = cash + reserved
        actual_delta = wallet_after - wallet_before
        per_event_ok.append(actual_delta == expected_delta)

    latest_mark = marks[-1] if marks else None
    unrealized_pnl = Fraction(0)
    for symbol, position in open_positions.items():
        if not latest_mark:
            continue
        mark_price = latest_mark.get("prices", {}).get(symbol)
        if mark_price is None:
            continue
        sign = Fraction(1) if position.side == "long" else Fraction(-1)
        unrealized_pnl += sign * position.qty * (_frac(mark_price) - position.entry_price)

    wallet_balance = cash + reserved
    current_equity = wallet_balance + unrealized_pnl

    return {
        "independent_check": True,
        "arithmetic_type": "fractions.Fraction (exact rational, does not import futures_ledger)",
        "all_events_conserve_wallet": all(per_event_ok),
        "trades_accounted_for": len(trades),
        "available_cash": str(cash),
        "reserved_margin": str(reserved),
        "wallet_balance": str(wallet_balance),
        "total_entry_fees": str(total_entry_fees),
        "total_exit_fees": str(total_exit_fees),
        "realized_pnl": str(total_realized_pnl),
        "unrealized_pnl": str(unrealized_pnl),
        "current_equity": str(current_equity),
    }


def _floats_agree(a: str, b: str, tol: float = 1e-6) -> bool:
    return abs(float(Fraction(a)) - float(b)) <= tol


def compare_with_primary(independent: dict[str, Any], primary_report: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "trades_accounted_for": independent["trades_accounted_for"] == primary_report["trades_accounted_for"],
        "realized_pnl": _floats_agree(independent["realized_pnl"], primary_report["realized_pnl"]),
        "wallet_balance": _floats_agree(independent["wallet_balance"], primary_report["wallet_balance"]),
        "reserved_margin": _floats_agree(independent["reserved_margin"], primary_report["reserved_margin"]),
        "unrealized_pnl": _floats_agree(independent["unrealized_pnl"], primary_report["unrealized_pnl"]),
        "current_equity": _floats_agree(independent["current_equity"], primary_report["current_equity"]),
        "all_events_conserve_wallet": independent["all_events_conserve_wallet"] and primary_report["conservation_residual_zero_for_every_event"],
    }
    return {"agreement_checks": checks, "all_agree": all(checks.values())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--initial-capital", required=True)
    parser.add_argument("--primary-report", required=True, type=Path)
    args = parser.parse_args()

    independent = verify(args.source, args.initial_capital)
    primary_report = json.loads(args.primary_report.read_text(encoding="utf-8"))
    comparison = compare_with_primary(independent, primary_report)

    result = {**independent, **comparison}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
