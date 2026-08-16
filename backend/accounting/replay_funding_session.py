#!/usr/bin/env python3
"""Deterministic replay of a quarantined funding_rate_zscore session journal
through the canonical Decimal ledger (accounting.futures_ledger).

Treats the source trades.jsonl executions (symbol, side, qty, price) as
evidence of what actually happened in the market, but ignores the source's
historical cash_remaining/reserved_margin/book.json values as accounting
inputs -- those are exactly what the forensic audit found unreliable. This
script recomputes cash, margin, fees, and realized P&L from scratch using
the invariant-checked open_position/close_position kernel.

Usage:
    python -m accounting.replay_funding_session \\
        --source paper_sessions/_quarantine/funding_live_accounting_invalid_<ts> \\
        --initial-capital 10000 \\
        --verify-only \\
        --report funding_live_reconciliation.json

--verify-only produces only the reconciliation report; it does not write a
new session directory. A separate --materialize-into flag (not implemented
here yet) would be needed to actually publish funding_live_replay_v1 -- per
the audit's promotion gate, that should only happen after this report and
the independent verifier (verify_replay_independent.py) agree exactly.

Known, explicitly documented limitation: the source journal has no
per-trade leverage field (margin was never correctly reserved historically,
which is the bug being replayed away), so a single LEVERAGE_ASSUMPTION is
applied uniformly across the whole replay. This affects only the margin/
capital-efficiency bookkeeping, not realized P&L, which depends solely on
qty * (exit_price - entry_price) * direction - fees, independent of
leverage. The assumption is recorded verbatim in the output report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .futures_ledger import (
    Account,
    AccountingInvariantError,
    Position,
    Side,
    close_position,
    dec,
    money,
    open_position,
)

LEVERAGE_ASSUMPTION = "2.0"  # session.json's leverage at entry_time / for the vast majority of this journal's life
FEE_RATE = "0.0005"  # session.json fee_rate, verified exact (fee_paid == notional * fee_rate) for all 22 source trades


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replay(source_dir: Path, initial_capital: str) -> dict[str, Any]:
    trades = _read_jsonl(source_dir / "trades.jsonl")
    marks = _read_jsonl(source_dir / "marks.jsonl")

    source_hashes = {}
    for name in ("session.json", "book.json", "trades.jsonl", "marks.jsonl"):
        p = source_dir / name
        if p.exists():
            source_hashes[name] = _sha256(p)

    account = Account(available_cash=money(initial_capital), reserved_margin=dec("0"))
    open_positions: dict[str, Position] = {}
    open_entry_fees: dict[str, Any] = {}  # symbol -> entry_fee, for positions still open
    events: list[dict[str, Any]] = []
    total_entry_fees_all = dec("0")  # every entry fee ever paid, open or closed
    total_exit_fees = dec("0")
    total_realized_pnl = dec("0")  # gross_pnl - entry_fee - exit_fee, closed positions only (matches compute_trade_stats convention)

    for i, t in enumerate(trades):
        symbol = t["symbol"]
        side_str = t["side"]
        qty = str(t["qty"])
        price = str(t["price"])
        current = open_positions.get(symbol)

        wallet_before = account.wallet_balance

        if current is None:
            # Opens a fresh position. BUY with no existing position -> long;
            # SELL with no existing position -> short (this journal never
            # partially fills or flips within one row -- verified during
            # the forensic audit).
            side = Side.LONG if side_str == "BUY" else Side.SHORT
            result = open_position(
                account=account,
                position_id=symbol,
                symbol=symbol,
                side=side,
                quantity=qty,
                execution_price=price,
                leverage=LEVERAGE_ASSUMPTION,
                fee_rate=FEE_RATE,
            )
            account = result.account
            open_positions[symbol] = result.position
            open_entry_fees[symbol] = result.entry_fee
            total_entry_fees_all = money(total_entry_fees_all + result.entry_fee)
            events.append({
                "event_index": i,
                "timestamp": t["timestamp"],
                "symbol": symbol,
                "action": "open",
                "side": side.value,
                "qty": str(result.position.quantity),
                "execution_price": str(price),
                "entry_fee": str(result.entry_fee),
                "wallet_before": str(wallet_before),
                "wallet_after": str(account.wallet_balance),
                "expected_wallet_delta": str(money(-result.entry_fee)),
                "actual_wallet_delta": str(money(account.wallet_balance - wallet_before)),
            })
        else:
            # Closes the existing position. This journal only ever fully
            # closes (qty matches the open position exactly) -- verified
            # during the forensic audit; partial-close is unused here.
            result = close_position(
                account=account,
                position=current,
                close_quantity=qty,
                execution_price=price,
                fee_rate=FEE_RATE,
            )
            account = result.account
            del open_positions[symbol]
            entry_fee = open_entry_fees.pop(symbol)
            total_exit_fees = money(total_exit_fees + result.exit_fee)
            # compute_trade_stats nets both entry and exit fee against a
            # closed position's realized P&L; the ledger's own net_pnl only
            # carries exit_fee (entry_fee already hit the wallet at open
            # time) -- entry_fee is subtracted here purely for this summary
            # statistic, not for wallet accounting (which is already correct
            # and checked per-event below).
            total_realized_pnl = money(total_realized_pnl + result.net_pnl - entry_fee)
            events.append({
                "event_index": i,
                "timestamp": t["timestamp"],
                "symbol": symbol,
                "action": "close",
                "reason": t.get("reason"),
                "qty": str(result.closed_quantity),
                "execution_price": str(price),
                "gross_pnl": str(result.gross_pnl),
                "exit_fee": str(result.exit_fee),
                "net_pnl": str(result.net_pnl),
                "released_margin": str(result.released_margin),
                "wallet_before": str(wallet_before),
                "wallet_after": str(account.wallet_balance),
                "expected_wallet_delta": str(money(result.gross_pnl - result.exit_fee)),
                "actual_wallet_delta": str(money(account.wallet_balance - wallet_before)),
            })

    # Every event's actual_wallet_delta must equal its expected_wallet_delta
    # -- that's the zero-conservation-residual requirement, checked per
    # event rather than only in aggregate.
    conservation_ok = all(e["actual_wallet_delta"] == e["expected_wallet_delta"] for e in events)

    latest_mark = marks[-1] if marks else None
    unrealized_pnl = dec("0")
    position_inventory = []
    for symbol, pos in open_positions.items():
        mark_price = None
        if latest_mark:
            mark_price = latest_mark.get("prices", {}).get(symbol)
        upnl = None
        if mark_price is not None:
            direction = dec("1") if pos.side is Side.LONG else dec("-1")
            upnl = money(direction * pos.quantity * (dec(str(mark_price)) - pos.entry_price))
            unrealized_pnl = money(unrealized_pnl + upnl)
        position_inventory.append({
            "symbol": symbol,
            "side": pos.side.value,
            "qty": str(pos.quantity),
            "entry_price": str(pos.entry_price),
            "margin_reserved": str(pos.margin_reserved),
            "mark_price": str(mark_price) if mark_price is not None else None,
            "unrealized_pnl": str(upnl) if upnl is not None else None,
        })

    current_equity = money(account.wallet_balance + unrealized_pnl)

    return {
        "status": "REPLAY_VALIDATION",
        "source_dir": str(source_dir),
        "source_file_hashes": source_hashes,
        "leverage_assumption": LEVERAGE_ASSUMPTION,
        "leverage_assumption_caveat": (
            "Source journal has no per-trade leverage field (margin was never "
            "correctly reserved historically -- that's the bug). A single "
            "leverage is applied uniformly across the replay. This affects only "
            "margin/capital-efficiency bookkeeping, not realized P&L (which "
            "depends solely on qty * (exit_price - entry_price) * direction - "
            "fees, independent of leverage)."
        ),
        "fee_rate_used": FEE_RATE,
        "funding_settlements": "none -- source journal recorded funding_rate as a signal input only, never settled as a cashflow (confirmed by audit); funding_net = 0 in this replay",
        "trades_accounted_for": len(trades),
        "events": events,
        "conservation_residual_zero_for_every_event": conservation_ok,
        "final_position_inventory": position_inventory,
        "available_cash": str(account.available_cash),
        "reserved_margin": str(account.reserved_margin),
        "wallet_balance": str(account.wallet_balance),
        "total_entry_fees_all_positions": str(total_entry_fees_all),
        "total_exit_fees": str(total_exit_fees),
        "total_fees": str(money(total_entry_fees_all + total_exit_fees)),
        "realized_pnl": str(total_realized_pnl),
        "realized_pnl_note": "entry+exit fees netted against closed positions only, matching paper_session.compute_trade_stats's convention -- still-open positions' entry fees are an unrealized cost, not charged here",
        "unrealized_pnl": str(unrealized_pnl),
        "current_equity": str(current_equity),
        "initial_capital": str(money(initial_capital)),
        "net_pnl_vs_initial_capital": str(money(current_equity - money(initial_capital))),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--initial-capital", required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    if not args.verify_only:
        raise SystemExit(
            "only --verify-only is implemented; materializing funding_live_replay_v1 "
            "is a deliberate separate step gated on this report and the independent "
            "verifier agreeing exactly"
        )

    report = replay(args.source, args.initial_capital)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(
        {k: report[k] for k in (
            "status", "trades_accounted_for", "conservation_residual_zero_for_every_event",
            "realized_pnl", "unrealized_pnl", "current_equity", "wallet_balance", "reserved_margin",
        )},
        indent=2,
    ))


if __name__ == "__main__":
    main()
