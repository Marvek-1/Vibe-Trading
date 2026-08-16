"""Profit Lab event-driven backtest engine.

Executes signals on t+1 close, tracks positions, closes by TP/SL/trailing/max-hold,
and tags every completed trade with entry_reason and exit_reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from profit_lab.models import LabRun, LabTrade, Signal


SignalStrategy = Callable[[pd.DataFrame], List[Signal]]


@dataclass
class _OpenPosition:
    symbol: str
    direction: int
    entry_price: float
    entry_time: datetime
    qty: float
    entry_reason: str
    entry_bar: int
    regime: str
    high_water: float
    low_water: float
    meta: Dict[str, Any] = field(default_factory=dict)


def run_lab_backtest(
    prices: pd.DataFrame,
    strategy: SignalStrategy,
    regimes: pd.DataFrame | None = None,
    initial_cash: float = 10_000.0,
    fee_rate: float = 0.001,
    slippage_rate: float = 0.0005,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trailing_stop: float | None = None,
    max_hold_bars: int | None = None,
    edge_filter: bool = False,
    min_edge_count: int = 10,
    min_edge_value: float = 0.0,
    bars_per_year: float = 365 * 24,
) -> LabRun:
    """Run a signal strategy and return a tagged LabRun."""
    if prices.empty or len(prices) < 2:
        raise ValueError("need at least two bars")

    symbols = list(prices.columns)
    cash = float(initial_cash)
    positions: Dict[str, _OpenPosition] = {}
    trades: List[LabTrade] = []
    equity_curve: List[float] = []
    timestamps: List[datetime] = []
    fees_paid = 0.0
    turnover = 0.0
    skipped_signals = 0
    edge_ledger: Dict[tuple[str, str], List[float]] = {}

    for i in range(len(prices) - 1):
        bar = prices.iloc[i]
        next_bar = prices.iloc[i + 1]
        timestamp = prices.index[i].to_pydatetime()
        next_timestamp = prices.index[i + 1].to_pydatetime()

        # Current equity before any action
        current_equity = cash + sum(positions[s].qty * bar[s] for s in positions)
        equity_curve.append(current_equity)
        timestamps.append(timestamp)

        # 1. Generate signals using bars up to and including i (no lookahead)
        raw_signals = strategy(prices.iloc[: i + 1])
        signals = []
        for sig in raw_signals:
            if sig.symbol not in symbols:
                continue
            if sig.side != 1:
                continue
            regime = "unknown"
            if regimes is not None:
                try:
                    regime = regimes.loc[next_timestamp, sig.symbol]
                except KeyError:
                    regime = "unknown"
                if pd.isna(regime):
                    regime = "unknown"
            if edge_filter:
                pnls = edge_ledger.get((sig.reason, regime), [])
                if len(pnls) >= min_edge_count:
                    expectancy = sum(pnls) / len(pnls)
                    if expectancy <= min_edge_value:
                        skipped_signals += 1
                        continue
            sig_with_regime = sig  # signals are immutable; carry regime via meta
            sig_with_regime.meta["regime"] = regime
            signals.append(sig_with_regime)

        wanted: Dict[str, int] = {}
        for sig in signals:
            wanted[sig.symbol] = sig.side

        # 2. Check risk exits first (on current bar close)
        to_close: List[Tuple[str, str, float]] = []
        for sym, pos in list(positions.items()):
            price = bar[sym]
            pos.high_water = max(pos.high_water, price)
            pos.low_water = min(pos.low_water, price)

            exit_reason = None
            pnl_pct = (price - pos.entry_price) / pos.entry_price * pos.direction

            if take_profit is not None and pnl_pct >= take_profit:
                exit_reason = "take_profit"
            elif stop_loss is not None and pnl_pct <= -stop_loss:
                exit_reason = "stop_loss"
            elif trailing_stop is not None and pos.high_water > pos.entry_price:
                trail_level = pos.high_water * (1.0 - trailing_stop)
                if price <= trail_level:
                    exit_reason = "trailing_stop"
            elif max_hold_bars is not None and (i - pos.entry_bar) >= max_hold_bars:
                exit_reason = "max_hold"

            if exit_reason:
                to_close.append((sym, exit_reason, price))

        for sym, reason, exit_price in to_close:
            pos = positions.pop(sym)
            t = _close_trade(pos, exit_price, next_timestamp, i + 1, fee_rate, slippage_rate, reason)
            trades.append(t)
            edge_ledger.setdefault((t.entry_reason, t.regime), []).append(t.net_pnl)
            cash = _update_cash_after_close(t, cash)
            fees_paid += t.fees
            turnover += t.qty * t.exit_price

        # 3. Execute signals on t+1 close.
        # A strategy's signal set IS the target portfolio. Any held symbol not in
        # the latest signals (or on the wrong side) is closed.
        for sym, pos in list(positions.items()):
            if wanted.get(sym, 0) != pos.direction:
                pos = positions.pop(sym)
                t = _close_trade(pos, next_bar[sym], next_timestamp, i + 1, fee_rate, slippage_rate, "signal_reverse")
                trades.append(t)
                edge_ledger.setdefault((t.entry_reason, t.regime), []).append(t.net_pnl)
                cash = _update_cash_after_close(t, cash)
                fees_paid += t.fees
                turnover += t.qty * t.exit_price

        # Open / scale positions for each wanted signal
        for sig in signals:
            if sig.symbol in positions and positions[sig.symbol].direction == sig.side:
                # Already aligned; rebalance not supported in this minimal engine
                continue
            if sig.side != 1:
                # Long-only in v1
                continue
            available = max(0.0, cash)
            notional = available * sig.size_fraction
            if notional <= 0:
                continue
            exec_price = next_bar[sig.symbol] * (1.0 + slippage_rate)
            fee = notional * fee_rate
            qty = (notional - fee) / exec_price
            if qty <= 0:
                continue
            regime = sig.meta.get("regime", "unknown")
            positions[sig.symbol] = _OpenPosition(
                symbol=sig.symbol,
                direction=1,
                entry_price=exec_price,
                entry_time=next_timestamp,
                qty=qty,
                entry_reason=sig.reason,
                entry_bar=i + 1,
                regime=regime,
                high_water=exec_price,
                low_water=exec_price,
                meta=dict(sig.meta),
            )
            cash -= notional
            fees_paid += fee
            turnover += notional

    # Final mark and close all positions at last bar
    final_bar = prices.iloc[-1]
    final_time = prices.index[-1].to_pydatetime()
    for sym, pos in list(positions.items()):
        t = _close_trade(pos, final_bar[sym], final_time, len(prices) - 1, fee_rate, slippage_rate, "end_of_test")
        trades.append(t)
        edge_ledger.setdefault((t.entry_reason, t.regime), []).append(t.net_pnl)
        cash = _update_cash_after_close(t, cash)
        fees_paid += t.fees
        turnover += t.qty * t.exit_price

    # All positions were closed above; final equity is cash plus any residual.
    final_equity = cash + sum(pos.qty * final_bar[pos.symbol] for pos in positions.values())
    equity_curve.append(final_equity)
    timestamps.append(final_time)

    return LabRun(
        strategy=strategy.__name__,
        trades=trades,
        equity_curve=equity_curve,
        timestamps=timestamps,
        fees_paid=fees_paid,
        turnover=turnover,
        initial_cash=initial_cash,
        final_equity=final_equity,
        skipped_signals=skipped_signals,
    )


def _close_trade(
    pos: _OpenPosition,
    exit_price_raw: float,
    exit_time: datetime,
    exit_bar: int,
    fee_rate: float,
    slippage_rate: float,
    exit_reason: str,
) -> LabTrade:
    if pos.direction > 0:
        exec_price = exit_price_raw * (1.0 - slippage_rate)
    else:
        exec_price = exit_price_raw * (1.0 + slippage_rate)
    notional = pos.qty * exec_price
    fee = notional * fee_rate
    gross_pnl = pos.direction * pos.qty * (exec_price - pos.entry_price)
    net_pnl = gross_pnl - fee
    margin_at_risk = pos.qty * pos.entry_price
    net_pnl_pct = net_pnl / margin_at_risk if margin_at_risk > 0 else 0.0
    return LabTrade(
        symbol=pos.symbol,
        direction=pos.direction,
        entry_time=pos.entry_time,
        exit_time=exit_time,
        entry_price=pos.entry_price,
        exit_price=exec_price,
        qty=pos.qty,
        leverage=1.0,
        entry_reason=pos.entry_reason,
        exit_reason=exit_reason,
        regime=pos.regime,
        gross_pnl=gross_pnl,
        fees=fee,
        net_pnl=net_pnl,
        net_pnl_pct=net_pnl_pct,
        hold_bars=exit_bar - pos.entry_bar,
        meta=pos.meta,
    )


def _update_cash_after_close(trade: LabTrade, cash: float) -> float:
    if trade.direction > 0:
        return cash + trade.qty * trade.exit_price - trade.fees
    return cash - trade.qty * trade.exit_price - trade.fees
