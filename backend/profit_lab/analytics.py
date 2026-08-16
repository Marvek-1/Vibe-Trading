"""Profit Lab analytics: expectancy and edge by entry reason."""

from __future__ import annotations

import math
from typing import Dict, List, Sequence

import numpy as np

from profit_lab.models import LabTrade, LabRun


def _sharpe(returns: Sequence[float], bars_per_year: float = 365 * 24) -> float:
    arr = np.array(returns, dtype=float)
    if len(arr) < 2 or arr.std(ddof=1) == 0:
        return 0.0
    return float(arr.mean() / arr.std(ddof=1) * math.sqrt(bars_per_year))


def _drawdown(equity: Sequence[float]) -> float:
    peak = equity[0]
    max_dd = 0.0
    for val in equity:
        peak = max(peak, val)
        dd = (peak - val) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def by_reason(trades: List[LabTrade]) -> Dict[str, Dict[str, float]]:
    """Return per-entry-reason expectancy statistics."""
    buckets: Dict[str, List[LabTrade]] = {}
    for t in trades:
        buckets.setdefault(t.entry_reason, []).append(t)

    result: Dict[str, Dict[str, float]] = {}
    for reason, bucket in buckets.items():
        pnls = [t.net_pnl for t in bucket]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total = sum(pnls)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses)) if losses else 1e-10
        pf = gross_profit / gross_loss if gross_loss > 0 else 0.0
        win_rate = len(wins) / len(bucket) if bucket else 0.0
        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0
        expectancy = win_rate * avg_win - (1 - win_rate) * abs(avg_loss)
        result[reason] = {
            "trades": len(bucket),
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "expectancy": round(expectancy, 4),
            "profit_factor": round(pf, 4),
            "net_pnl": round(total, 4),
        }
    return dict(sorted(result.items(), key=lambda x: x[1]["expectancy"], reverse=True))


def summary(run: LabRun, bars_per_year: float = 365 * 24) -> Dict[str, float]:
    """Portfolio-level summary."""
    trades = run.trades
    if not trades:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "total_return": 0.0,
            "fees_paid": 0.0,
        }
    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(trades)
    pf = (sum(wins) / abs(sum(losses))) if losses else (sum(wins) if wins else 0.0)
    pf = pf if pf != 0 else float("inf") if sum(wins) > 0 else 0.0
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    expectancy = win_rate * avg_win - (1 - win_rate) * abs(avg_loss)

    equity = run.equity_curve
    returns = [(equity[i] / equity[i - 1]) - 1 for i in range(1, len(equity))]
    total_return = (run.final_equity - run.initial_cash) / run.initial_cash

    return {
        "trades": len(trades),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "expectancy": round(expectancy, 4),
        "profit_factor": round(pf, 4) if not math.isinf(pf) else 99.99,
        "sharpe": round(_sharpe(returns, bars_per_year), 4),
        "max_drawdown": round(_drawdown(equity), 4),
        "total_return": round(total_return, 4),
        "fees_paid": round(run.fees_paid, 4),
        "turnover": round(run.turnover, 4),
    }


def by_reason_and_regime(trades: List[LabTrade]) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Return nested {reason: {regime: stats}} for expectancy matrix."""
    matrix: Dict[str, Dict[str, List[LabTrade]]] = {}
    for t in trades:
        matrix.setdefault(t.entry_reason, {}).setdefault(t.regime, []).append(t)
    result: Dict[str, Dict[str, Dict[str, float]]] = {}
    for reason, regimes in matrix.items():
        result[reason] = {}
        for regime, bucket in regimes.items():
            pnls = [t.net_pnl for t in bucket]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            gross_profit = sum(wins)
            gross_loss = abs(sum(losses)) if losses else 1e-10
            pf = gross_profit / gross_loss if gross_loss > 0 else 0.0
            win_rate = len(wins) / len(bucket) if bucket else 0.0
            avg_win = float(np.mean(wins)) if wins else 0.0
            avg_loss = float(np.mean(losses)) if losses else 0.0
            expectancy = win_rate * avg_win - (1 - win_rate) * abs(avg_loss)
            result[reason][regime] = {
                "trades": len(bucket),
                "win_rate": round(win_rate, 4),
                "avg_win": round(avg_win, 4),
                "avg_loss": round(avg_loss, 4),
                "expectancy": round(expectancy, 4),
                "profit_factor": round(pf, 4) if not math.isinf(pf) else 99.99,
                "net_pnl": round(sum(pnls), 4),
            }
    return result


def trade_ledger(run: LabRun, n: int = 8) -> str:
    """Human-readable immutable trade ledger (first + last n trades)."""
    lines = [
        "",
        "=== Trade Ledger (first/last sample) ===",
        f"{'ID':>4s} {'Sym':<10s} {'Margin':>8s} {'Lev':>4s} {'Entry':>12s} {'Exit':>12s} {'Gross':>10s} {'Fees':>8s} {'Funding':>8s} {'Net':>10s} {'ROI%':>7s} {'Reason':<18s} {'Exit':<14s}",
    ]
    sample = list(run.trades[: n // 2]) + list(run.trades[-(n - n // 2):]) if len(run.trades) > n else run.trades
    for t in sample:
        lines.append(
            f"{t.trade_id:>4d} {t.symbol:<10s} ${t.margin:>7.2f} {int(t.leverage):>3d}x "
            f"${t.entry_price:>11.4f} ${t.exit_price:>11.4f} "
            f"${t.gross_pnl:>9.2f} ${t.fees:>7.2f} ${t.funding_paid:>7.4f} ${t.net_pnl:>9.2f} "
            f"{t.net_pnl_pct*100:>6.2f}% {t.entry_reason:<18s} {t.exit_reason:<14s}"
        )
    if run.trades:
        total = sum(t.net_pnl for t in run.trades)
        fees = sum(t.fees for t in run.trades)
        funding = sum(t.funding_paid for t in run.trades)
        gross = sum(t.gross_pnl for t in run.trades)
        lines.append(f"{'':>4s} {'TOTAL':<10s} {'':>8s} {'':>4s} {'':>12s} {'':>12s} ${gross:>9.2f} ${fees:>7.2f} ${funding:>7.4f} ${total:>9.2f}")
    return "\n".join(lines)


def report(run: LabRun) -> str:
    """Human-readable lab report."""
    lines = [
        f"Strategy: {run.strategy}",
        f"Initial: ${run.initial_cash:,.2f}  Final: ${run.final_equity:,.2f}  Cash: ${run.cash_curve[-1]:,.2f}  Reserved: ${run.reserved_margin:,.2f}",
        "",
        "=== Portfolio Summary ===",
    ]
    for k, v in summary(run).items():
        lines.append(f"  {k:20s}: {v}")
    lines.append("")
    lines.append("=== Expectancy by Entry Reason ===")
    reasons = by_reason(run.trades)
    if not reasons:
        lines.append("  No trades")
    for reason, stats in reasons.items():
        line = f"  {reason:24s} trades={int(stats['trades']):4d}  win={stats['win_rate']:.2%}  exp=${stats['expectancy']:.2f}  pf={stats['profit_factor']:.2f}  pnl=${stats['net_pnl']:.2f}"
        lines.append(line)
    lines.append("")
    lines.append("=== Expectancy by Entry Reason + Regime ===")
    matrix = by_reason_and_regime(run.trades)
    if not matrix:
        lines.append("  No trades")
    for reason, regmap in matrix.items():
        lines.append(f"  {reason}")
        for regime, stats in sorted(regmap.items()):
            line = f"    {regime:26s} trades={int(stats['trades']):4d}  win={stats['win_rate']:.2%}  exp=${stats['expectancy']:.2f}  pf={stats['profit_factor']:.2f}  pnl=${stats['net_pnl']:.2f}"
            lines.append(line)
    lines.append(trade_ledger(run, n=8))
    return "\n".join(lines)
