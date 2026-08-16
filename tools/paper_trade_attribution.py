#!/usr/bin/env python3
"""Per-trade cost/edge decomposition for a paper-trading session.

Answers, from real receipted trades/marks only (no live network calls, no
new sessions): is the signal negative before costs, is a positive gross
edge consumed by fees/turnover, are losses concentrated in specific
symbols, and are losses concentrated in specific volatility regimes.

Every number here comes from trades.jsonl / marks.jsonl / session.json on
disk -- nothing is estimated or assumed if it can be computed instead.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agent"
import sys  # noqa: E402

sys.path.insert(0, str(AGENT_DIR))
import paper_session as ps  # noqa: E402


def _parse_iso(value: str):
    return ps._parse_iso(value)


def load_session_data(session_dir: Path) -> dict[str, Any]:
    session = ps._load_session(session_dir)
    book = ps._load_book(session_dir)
    trades = ps._read_jsonl(session_dir / "trades.jsonl")
    marks = ps._read_jsonl(session_dir / "marks.jsonl")
    trade_stats = ps.compute_trade_stats(trades)
    return {"session": session, "book": book, "trades": trades, "marks": marks, "trade_stats": trade_stats}


def _excursion(marks: list[dict[str, Any]], symbol: str, entry_time: str, exit_time: str, entry_price: float) -> dict[str, float | None]:
    """Max favorable / adverse excursion for one symbol's price between two
    timestamps, as a fraction of entry_price. None if no marks fall in the
    window (can't be computed, not assumed to be 0)."""
    if not entry_time or entry_price <= 0:
        return {"mfe_pct": None, "mae_pct": None}
    t0, t1 = _parse_iso(entry_time), _parse_iso(exit_time)
    prices = [
        m["prices"][symbol]
        for m in marks
        if symbol in m.get("prices", {}) and t0 <= _parse_iso(m["timestamp"]) <= t1
    ]
    if not prices:
        return {"mfe_pct": None, "mae_pct": None}
    return {
        "mfe_pct": (max(prices) - entry_price) / entry_price,
        "mae_pct": (min(prices) - entry_price) / entry_price,
    }


def build_trade_attribution(data: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per closed (SELL) trade -- the only trades with a realized
    gross/net P&L to attribute. BUY (entry/rebalance-add) trades only ever
    grow a position; they carry no standalone P&L of their own."""
    marks = data["marks"]
    rows: list[dict[str, Any]] = []
    for trade in data["trade_stats"]["trades"]:
        if trade["side"] != "SELL":
            continue
        entry_time = trade.get("entry_time")
        exit_time = trade["timestamp"]
        entry_price = trade.get("entry_price", 0.0) or 0.0
        hold_hours = None
        if entry_time:
            hold_hours = (_parse_iso(exit_time) - _parse_iso(entry_time)).total_seconds() / 3600.0
        excursion = _excursion(marks, trade["symbol"], entry_time, exit_time, entry_price)
        rows.append({
            "timestamp": exit_time,
            "symbol": trade["symbol"],
            "notional": trade.get("notional", 0.0),
            "entry_price": entry_price,
            "exit_price": trade["price"],
            "gross_pnl": trade.get("gross_pnl"),
            "entry_fee_allocated": trade.get("entry_fee_allocated"),
            "exit_fee": trade.get("fee_paid", 0.0),
            "total_fees": trade.get("total_fees"),
            "net_pnl": trade.get("net_pnl"),
            "hold_hours": hold_hours,
            "mfe_pct": excursion["mfe_pct"],
            "mae_pct": excursion["mae_pct"],
            "exit_reason": trade.get("reason", "unknown"),
        })
    return rows


def symbol_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row)
    out = []
    for symbol, sym_rows in sorted(by_symbol.items()):
        net = [r["net_pnl"] for r in sym_rows if r["net_pnl"] is not None]
        gross = [r["gross_pnl"] for r in sym_rows if r["gross_pnl"] is not None]
        wins = sum(1 for v in net if v > 0)
        out.append({
            "symbol": symbol,
            "closed_trades": len(sym_rows),
            "gross_pnl": sum(gross) if gross else 0.0,
            "net_pnl": sum(net) if net else 0.0,
            "win_rate": wins / len(net) if net else None,
        })
    return out


def volatility_regime_diagnostics(data: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Bucket days by realized market volatility (median split, using every
    symbol's mark-to-mark returns pooled together as a market-wide proxy --
    this session has no single benchmark instrument), then compare average
    daily net P&L in high-vol vs low-vol days.

    This is an approximation, not the full regime classifier the original
    plan asked for (trend/sideways/high-vol) -- that requires infrastructure
    that does not exist in this repo yet. Flagged explicitly rather than
    faked.
    """
    marks = data["marks"]
    by_day_returns: dict[str, list[float]] = {}
    prev_prices: dict[str, float] = {}
    for m in marks:
        day = m["timestamp"][:10]
        for symbol, price in m.get("prices", {}).items():
            prev = prev_prices.get(symbol)
            if prev:
                by_day_returns.setdefault(day, []).append(abs(price - prev) / prev)
            prev_prices[symbol] = price

    daily_vol = {day: statistics.mean(rets) for day, rets in by_day_returns.items() if rets}
    if len(daily_vol) < 2:
        return {"note": "insufficient distinct days for a vol-regime split", "days_available": len(daily_vol)}

    median_vol = statistics.median(daily_vol.values())
    high_vol_days = {d for d, v in daily_vol.items() if v >= median_vol}

    by_day_net: dict[str, float] = {}
    for row in rows:
        if row["net_pnl"] is None:
            continue
        by_day_net.setdefault(row["timestamp"][:10], 0.0)
        by_day_net[row["timestamp"][:10]] += row["net_pnl"]

    high_vol_net = [pnl for day, pnl in by_day_net.items() if day in high_vol_days]
    low_vol_net = [pnl for day, pnl in by_day_net.items() if day not in high_vol_days]
    return {
        "median_daily_vol_pct": median_vol,
        "high_vol_days": sorted(high_vol_days),
        "low_vol_days": sorted(set(daily_vol) - high_vol_days),
        "high_vol_avg_daily_net_pnl": statistics.mean(high_vol_net) if high_vol_net else None,
        "low_vol_avg_daily_net_pnl": statistics.mean(low_vol_net) if low_vol_net else None,
        "daily_net_pnl": by_day_net,
    }


def build_equity_comparison(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-mark strategy NAV vs. a static equal-weight buy-and-hold NAV vs.
    flat cash -- the only way to tell whether rebalancing helped, hurt, or
    was neutral relative to just holding the entry basket.

    The buy-hold leg is sized identically to the real session's own entry
    trade (same investable_cash/(1+fee_rate) split, same entry_prices,
    charged the same one-time entry fee) so the two NAVs start from the
    same point and diverge only because of what happened *after* entry --
    the exact quantity a "did rebalancing help" question needs.
    """
    session = data["session"]
    marks = data["marks"]
    symbols = session["symbols"]
    initial_cash = float(session["initial_cash"])
    fee_rate = float(session.get("fee_rate", 0.0) or 0.0)
    entry_prices = session.get("entry_prices")
    if not entry_prices:
        return []

    investable_cash = initial_cash / (1 + fee_rate) if fee_rate else initial_cash
    per_symbol_cash = investable_cash / len(symbols)
    buy_hold_qty = {s: per_symbol_cash / entry_prices[s] for s in symbols}
    buy_hold_cash = initial_cash - per_symbol_cash * len(symbols) * (1 + fee_rate)

    rows = []
    for m in marks:
        prices = m.get("prices", {})
        if not all(s in prices for s in symbols):
            continue  # incomplete mark -- excluded, not assumed complete
        buy_hold_nav = buy_hold_cash + sum(buy_hold_qty[s] * prices[s] for s in symbols)
        rows.append({
            "timestamp": m["timestamp"],
            "strategy_nav": m["equity"],
            "buy_hold_nav": buy_hold_nav,
            "cash_nav": initial_cash,
        })
    return rows


def benchmark_attribution(data: dict[str, Any], rows: list[dict[str, Any]], equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare strategy NAV to a static equal-weight buy-hold NAV, and check
    ledger integrity independently of both.

    Two different things are computed here, and they must not be confused:

    1. A DECOMPOSITION of active_return_dollars (strategy_nav - buy_hold_nav)
       into realized rebalance P&L (already banked via closing/SELL trades)
       plus unrealized allocation drift (the mark-to-market value, at
       current prices, of the strategy currently holding different
       quantities than the static buy-hold basket would). This is an
       algebraic identity -- realized + unrealized_drift - fees always
       equals active_return_dollars exactly, by construction, because both
       sides are computed from the same book/price state. It is NOT an
       independent check; treat "residual" here as an accounting split, not
       evidence of correctness. (An earlier version of this function
       mistakenly reported a nonzero residual on every session because it
       omitted the unrealized_drift term entirely -- that was a bug in the
       formula, not a real reconciliation failure.)

    2. A genuinely INDEPENDENT integrity check: replaying every trade's cash
       flow from initial_cash should reproduce book.json's actual
       cash_remaining. This uses only trades.jsonl and never touches
       marks.jsonl/book.json's own cash figure until the final comparison,
       so a mismatch here is a real signal of ledger drift -- this is the
       one flagged against tolerance.
    """
    if not equity_curve:
        return {"note": "no complete-price marks available to build a buy-hold benchmark"}

    session = data["session"]
    symbols = session["symbols"]
    initial_cash = float(session["initial_cash"])
    latest = equity_curve[-1]
    strategy_nav = latest["strategy_nav"]
    buy_hold_nav = latest["buy_hold_nav"]
    cash_nav = latest["cash_nav"]

    basket_return = (buy_hold_nav - cash_nav) / cash_nav if cash_nav else None
    strategy_return = (strategy_nav - initial_cash) / initial_cash if initial_cash else None
    active_return_dollars = strategy_nav - buy_hold_nav
    active_return_pct = active_return_dollars / initial_cash if initial_cash else None

    all_trades = data["trades"]
    total_fees = sum(float(t.get("fee_paid", 0.0) or 0.0) for t in all_trades)
    entry_fees = sum(float(t.get("fee_paid", 0.0) or 0.0) for t in all_trades if t.get("reason") == "entry")
    turnover_drag = total_fees - entry_fees  # rebalance-only fees; entry fee is common to both legs and cancels out

    realized_alpha = sum(r["gross_pnl"] for r in rows if r["gross_pnl"] is not None)
    # unrealized_drift is *defined* as whatever makes the identity close --
    # see docstring: this is a decomposition, not an independent estimate.
    unrealized_allocation_drift = active_return_dollars - realized_alpha + turnover_drag

    book = data["book"]
    trade_stats = data["trade_stats"]
    latest_prices = data["marks"][-1].get("prices", {}) if data["marks"] else {}
    position = ps._compute_unrealized_position_pnl(trade_stats["by_symbol"], latest_prices=latest_prices)
    position_values = sum(book.get("positions", {}).get(s, 0.0) * latest_prices.get(s, 0.0) for s in symbols)
    gross_exposure = position_values / strategy_nav if strategy_nav else None

    # Independent check: reconstruct cash purely from trades.jsonl cash
    # flows and compare against book.json's own cash_remaining.
    reconstructed_cash = initial_cash
    for t in all_trades:
        notional = float(t.get("notional", 0.0) or 0.0)
        fee = float(t.get("fee_paid", 0.0) or 0.0)
        if t["side"] == "BUY":
            reconstructed_cash -= notional + fee
        else:
            reconstructed_cash += notional - fee
    book_cash = float(book.get("cash_remaining", 0.0))
    cash_residual = reconstructed_cash - book_cash
    tolerance = max(ps.RECONCILIATION_ABS_TOLERANCE, abs(strategy_nav) * ps.RECONCILIATION_REL_TOLERANCE)

    return {
        "strategy_nav": strategy_nav,
        "static_equal_weight_buy_hold_nav": buy_hold_nav,
        "cash_nav": cash_nav,
        "basket_return": basket_return,
        "strategy_return": strategy_return,
        "active_return_dollars": active_return_dollars,
        "active_return_pct": active_return_pct,
        "fee_drag_total": total_fees,
        "turnover_drag_rebalance_fees_only": turnover_drag,
        "active_return_decomposition": {
            "realized_alpha_from_closed_trades": realized_alpha,
            "unrealized_allocation_drift": unrealized_allocation_drift,
            "turnover_drag": turnover_drag,
            "note": "realized + unrealized_drift - turnover_drag == active_return_dollars by construction; this is a split, not a check",
        },
        "unrealized_pnl": position["unrealized_pnl"],
        "realized_pnl": trade_stats["overall"]["realized_pnl"],
        # long-only book: gross and net exposure are identical (no shorts).
        "gross_exposure_pct_of_equity": gross_exposure,
        "net_exposure_pct_of_equity": gross_exposure,
        "ledger_integrity_check": {
            "description": "cash reconstructed from trades.jsonl cash flows vs. book.json's actual cash_remaining -- genuinely independent of the NAV/buy-hold comparison above",
            "reconstructed_cash": reconstructed_cash,
            "book_cash_remaining": book_cash,
            "residual": cash_residual,
            "tolerance": tolerance,
            "within_tolerance": abs(cash_residual) <= tolerance,
        },
        "interpretation": (
            "rebalancing_added_value_but_did_not_overcome_beta" if active_return_dollars > tolerance and strategy_return is not None and strategy_return < 0
            else "rebalancing_contributed_little" if abs(active_return_dollars) <= tolerance
            else "rebalancing_destroyed_value" if active_return_dollars < -tolerance
            else "rebalancing_added_value"
        ),
    }


def summarize(session_dir: Path, data: dict[str, Any], rows: list[dict[str, Any]], equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
    overall = data["trade_stats"]["overall"]
    all_trades = data["trades"]
    turnover = sum(float(t.get("notional", 0.0) or 0.0) for t in all_trades)
    total_fees = overall["fees_paid"]
    gross_total = sum(r["gross_pnl"] for r in rows if r["gross_pnl"] is not None)
    net_total = sum(r["net_pnl"] for r in rows if r["net_pnl"] is not None)
    marks = data["marks"]
    current_equity = marks[-1]["equity"] if marks else None

    sym_diag = symbol_diagnostics(rows)
    worst_symbol = min(sym_diag, key=lambda s: s["net_pnl"]) if sym_diag else None
    total_negative_net = sum(s["net_pnl"] for s in sym_diag if s["net_pnl"] < 0)
    worst_symbol_share = (
        worst_symbol["net_pnl"] / total_negative_net
        if worst_symbol and total_negative_net < 0
        else None
    )

    verdict = {
        "signal_negative_before_costs": gross_total < 0,
        "gross_edge_positive_but_cost_consumed": gross_total > 0 and net_total < 0,
        "single_symbol_dominates_losses": bool(
            worst_symbol_share is not None and worst_symbol_share >= 0.5
        ),
        "worst_symbol": worst_symbol["symbol"] if worst_symbol else None,
        "worst_symbol_share_of_losses": worst_symbol_share,
    }

    return {
        "session_id": session_dir.name,
        "session_symbols": data["session"]["symbols"],
        "rebalance_interval_hours": data["session"]["rebalance_interval_hours"],
        "fee_rate": data["session"].get("fee_rate"),
        "min_rebalance_notional": data["session"].get("min_rebalance_notional"),
        "risk_config": data["session"].get("risk_config"),
        "initial_cash": data["session"]["initial_cash"],
        "current_equity": current_equity,
        "closed_trade_count": len(rows),
        "turnover": turnover,
        "total_fees_paid": total_fees,
        "gross_pnl_of_closed_trades": gross_total,
        "net_pnl_of_closed_trades": net_total,
        "cost_drag_pct_of_gross": (
            total_fees / abs(gross_total) if gross_total not in (0, None) else None
        ),
        "symbol_diagnostics": sym_diag,
        "volatility_regime_diagnostics": volatility_regime_diagnostics(data, rows),
        "benchmark_attribution": benchmark_attribution(data, rows, equity_curve),
        "verdict": verdict,
    }


def write_reports(
    session_dir: Path,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    equity_curve: list[dict[str, Any]],
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    session_out = output_dir / session_dir.name
    session_out.mkdir(parents=True, exist_ok=True)

    with (session_out / "trade_attribution.csv").open("w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    with (session_out / "equity_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        if equity_curve:
            writer = csv.DictWriter(f, fieldnames=list(equity_curve[0].keys()))
            writer.writeheader()
            writer.writerows(equity_curve)

    (session_out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", action="append", default=[], help="repeatable")
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "reports" / "profit_recovery",
    )
    args = parser.parse_args()

    if not args.session_dir:
        parser.error("provide at least one --session-dir")

    all_summaries = []
    for raw in args.session_dir:
        session_dir = Path(raw)
        data = load_session_data(session_dir)
        rows = build_trade_attribution(data)
        equity_curve = build_equity_comparison(data)
        summary = summarize(session_dir, data, rows, equity_curve)
        write_reports(session_dir, rows, summary, equity_curve, args.output_dir)
        all_summaries.append(summary)
        print(json.dumps({"session_id": summary["session_id"], "verdict": summary["verdict"],
                           "net_pnl_of_closed_trades": summary["net_pnl_of_closed_trades"],
                           "current_equity": summary["current_equity"],
                           "benchmark_attribution": summary["benchmark_attribution"]}, indent=2, default=str))

    (args.output_dir / "all_sessions_summary.json").write_text(
        json.dumps(all_summaries, indent=2, default=str), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
