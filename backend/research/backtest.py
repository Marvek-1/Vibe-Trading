"""Cost-aware portfolio backtester with paper-session cash semantics.

Execution model mirrors the live paper ledger deliberately:
  - signals computed on bar t execute on bar t+1 close (no lookahead)
  - sells execute first; buys share remaining cash proportionally
  - cash can never go negative; fees and slippage charged on every notional
Metrics are computed on the resulting equity curve only -- no strategy is
allowed to report anything the ledger cannot reproduce.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

# Strategy contract: prices seen so far (rows <= t) -> target weights by symbol.
# Weights must be >= 0 and sum to <= 1.0; the remainder sits in cash.
Strategy = Callable[[pd.DataFrame], dict[str, float]]

MIN_TRADE_NOTIONAL = 1.0


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: int = 0
    closed_trades: int = 0
    turnover: float = 0.0
    fees_paid: float = 0.0
    realized_pnls: list[float] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def run_backtest(
    prices: pd.DataFrame,
    strategy: Strategy,
    initial_cash: float = 10_000.0,
    fee_rate: float = 0.001,
    slippage_rate: float = 0.0005,
    rebalance_every: int = 1,
    bars_per_year: float = 365.0 * 24,
    band_fraction: float = 0.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> BacktestResult:
    if prices.empty or len(prices) < 2:
        raise ValueError("need at least two bars to backtest")

    symbols = list(prices.columns)
    cash = float(initial_cash)
    positions = {symbol: 0.0 for symbol in symbols}
    entry_prices: dict[str, float] = {}
    cost_bases: dict[str, float] = {}
    cost_rate = fee_rate + slippage_rate
    equity_points: list[float] = []
    trades = 0
    closed_trades = 0
    turnover = 0.0
    fees_paid = 0.0
    realized_pnls: list[float] = []
    pending_weights: dict[str, float] | None = None

    for i in range(len(prices)):
        bar = prices.iloc[i]
        equity = cash + sum(positions[s] * bar[s] for s in symbols)

        # Exit-salvage: enforce stop-loss and take-profit for any held position,
        # regardless of whether a rebalance is scheduled this bar.
        if stop_loss is not None or take_profit is not None:
            for s in list(symbols):
                if positions[s] <= 0 or s not in entry_prices:
                    continue
                stop_triggered = stop_loss is not None and bar[s] < entry_prices[s] * (1.0 - stop_loss)
                profit_triggered = take_profit is not None and bar[s] > entry_prices[s] * (1.0 + take_profit)
                if stop_triggered or profit_triggered:
                    sold_qty = positions[s]
                    notional = sold_qty * bar[s]
                    cost = notional * cost_rate
                    realized_pnls.append(notional - cost - sold_qty * cost_bases[s])
                    cash += notional - cost
                    positions[s] = 0.0
                    del entry_prices[s]
                    del cost_bases[s]
                    trades += 1
                    closed_trades += 1
                    turnover += notional
                    fees_paid += cost
                    if pending_weights is not None:
                        pending_weights[s] = 0.0

        equity = cash + sum(positions[s] * bar[s] for s in symbols)

        if pending_weights is not None:
            deltas = {
                s: pending_weights.get(s, 0.0) * equity - positions[s] * bar[s]
                for s in symbols
            }
            min_notional = max(MIN_TRADE_NOTIONAL, band_fraction * equity)
            for s, delta in deltas.items():  # sells first: they raise cash
                if delta < -min_notional:
                    notional = -delta
                    cost = notional * cost_rate
                    sold_qty = notional / bar[s]
                    realized_pnls.append(notional - cost - sold_qty * cost_bases[s])
                    positions[s] -= sold_qty
                    cash += notional - cost
                    trades += 1
                    closed_trades += 1
                    turnover += notional
                    fees_paid += cost
            buy_cost = sum(d * (1 + cost_rate) for d in deltas.values() if d > min_notional)
            scale = min(1.0, max(cash, 0.0) / buy_cost) if buy_cost > 0 else 0.0
            for s, delta in deltas.items():
                if delta > min_notional and scale > 0:
                    notional = delta * scale
                    cost = notional * cost_rate
                    total = notional + cost
                    if total > cash:
                        continue
                    new_qty = notional / bar[s]
                    old_qty = positions[s]
                    old_entry = entry_prices.get(s, bar[s])
                    old_cost_basis = cost_bases.get(s, bar[s] * (1.0 + cost_rate))
                    if old_qty + new_qty > 0:
                        entry_prices[s] = (old_qty * old_entry + new_qty * bar[s]) / (old_qty + new_qty)
                        cost_bases[s] = (old_qty * old_cost_basis + total) / (old_qty + new_qty)
                    positions[s] += new_qty
                    cash -= total
                    trades += 1
                    turnover += notional
                    fees_paid += cost
            equity = cash + sum(positions[s] * bar[s] for s in symbols)
            pending_weights = None

        equity_points.append(equity)

        if i % rebalance_every == 0 and i < len(prices) - 1:
            weights = strategy(prices.iloc[: i + 1])
            if weights:
                total = sum(max(w, 0.0) for w in weights.values())
                if total > 1.0 + 1e-9:
                    weights = {s: max(w, 0.0) / total for s, w in weights.items()}
                pending_weights = {s: max(weights.get(s, 0.0), 0.0) for s in symbols}
            else:
                pending_weights = None

    curve = pd.Series(equity_points, index=prices.index)
    return BacktestResult(
        equity_curve=curve,
        trades=trades,
        closed_trades=closed_trades,
        turnover=turnover,
        fees_paid=fees_paid,
        realized_pnls=realized_pnls,
        metrics=compute_metrics(curve, bars_per_year, realized_pnls),
    )


def compute_metrics(
    equity_curve: pd.Series,
    bars_per_year: float,
    realized_pnls: list[float] | None = None,
) -> dict[str, float]:
    returns = equity_curve.pct_change().dropna()
    total_return = equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0
    running_max = equity_curve.cummax()
    max_drawdown = float(((equity_curve - running_max) / running_max).min())
    if len(returns) > 1 and returns.std() > 0:
        sharpe = float(returns.mean() / returns.std() * math.sqrt(bars_per_year))
    else:
        sharpe = 0.0
    positive_returns = float(returns[returns > 0.0].sum())
    negative_returns = float(-returns[returns < 0.0].sum())
    return_profit_factor = positive_returns / negative_returns if negative_returns > 0.0 else float("inf")
    closed_pnls = realized_pnls or []
    gross_profit = sum(value for value in closed_pnls if value > 0.0)
    gross_loss = -sum(value for value in closed_pnls if value < 0.0)
    profit_factor = gross_profit / gross_loss if gross_loss > 0.0 else (float("inf") if gross_profit > 0.0 else 0.0)
    expectancy = sum(closed_pnls) / len(closed_pnls) if closed_pnls else 0.0
    years = len(returns) / bars_per_year if bars_per_year else 0.0
    initial = equity_curve.iloc[0]
    final = equity_curve.iloc[-1]
    if years > 0 and final > 0 and initial > 0:
        log_ratio = math.log(final / initial)
        exponent = log_ratio / years
        if exponent > 700:
            cagr = float("inf")
        elif exponent < -700:
            cagr = float("-inf")
        else:
            cagr = math.exp(exponent) - 1.0
    else:
        cagr = float(total_return)
    return {
        "total_return": float(total_return),
        "cagr": cagr,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "profit_factor": profit_factor,
        "return_profit_factor": return_profit_factor,
        "expectancy": expectancy,
        "closed_trades": float(len(closed_pnls)),
        "final_equity": float(equity_curve.iloc[-1]),
        "bars": int(len(equity_curve)),
    }
