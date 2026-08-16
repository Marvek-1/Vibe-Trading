"""Futures-style Profit Lab backtest engine.

Each signal opens an isolated-margin trade with fixed margin and leverage.
Completed trades are immutable ledger entries. Session P&L is the sum of realized
trade P&L. Equity = cash + reserved margin + open unrealized P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Set, Tuple

import pandas as pd

from profit_lab.models import LabRun, LabTrade, Signal


SignalStrategy = Callable[[pd.DataFrame], List[Signal]]
FUNDING_HOURS = {0, 8, 16}


@dataclass
class _OpenPosition:
    trade_id: int
    symbol: str
    direction: int
    entry_price: float
    qty: float
    margin: float
    leverage: float
    notional: float
    entry_time: datetime
    entry_bar: int
    entry_reason: str
    regime: str
    high_water: float
    low_water: float
    funding_paid: float
    funding_applied: Set[Tuple[str, date, int]] = field(default_factory=set)
    meta: Dict[str, Any] = field(default_factory=dict)


def _entry_exec_price(price: float, slippage_rate: float) -> float:
    return price * (1.0 + slippage_rate)


def _exit_exec_price(price: float, slippage_rate: float) -> float:
    return price * (1.0 - slippage_rate)


def _apply_funding(
    pos: _OpenPosition,
    timestamp: datetime,
    funding_rate_8h: float,
    cash: float,
) -> float:
    if funding_rate_8h == 0.0:
        return cash
    hour = timestamp.hour
    if hour not in FUNDING_HOURS:
        return cash
    key = (pos.symbol, timestamp.date(), hour)
    if key in pos.funding_applied:
        return cash
    pos.funding_applied.add(key)
    # longs pay when rate > 0, shorts receive (cash increases)
    fee = pos.notional * funding_rate_8h * pos.direction
    pos.funding_paid += fee
    return cash - fee


def _close_trade(
    pos: _OpenPosition,
    exit_price_raw: float,
    exit_time: datetime,
    exit_bar: int,
    fee_rate: float,
    slippage_rate: float,
    exit_reason: str,
) -> LabTrade:
    exec_price = _exit_exec_price(exit_price_raw, slippage_rate)
    exit_notional = pos.qty * exec_price
    entry_fee = pos.notional * fee_rate
    exit_fee = exit_notional * fee_rate
    gross_pnl = pos.direction * pos.qty * (exec_price - pos.entry_price)
    fees = entry_fee + exit_fee
    net_pnl = gross_pnl - fees - pos.funding_paid
    return LabTrade(
        trade_id=pos.trade_id,
        symbol=pos.symbol,
        direction=pos.direction,
        entry_time=pos.entry_time,
        exit_time=exit_time,
        entry_price=pos.entry_price,
        exit_price=exec_price,
        qty=pos.qty,
        margin=pos.margin,
        leverage=pos.leverage,
        notional=pos.notional,
        entry_reason=pos.entry_reason,
        exit_reason=exit_reason,
        regime=pos.regime,
        gross_pnl=gross_pnl,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        fees=fees,
        funding_paid=pos.funding_paid,
        net_pnl=net_pnl,
        net_pnl_pct=net_pnl / pos.margin if pos.margin > 0 else 0.0,
        hold_bars=exit_bar - pos.entry_bar,
        meta=dict(pos.meta),
    )


def run_lab_backtest(
    prices: pd.DataFrame,
    strategy: SignalStrategy,
    regimes: pd.DataFrame | None = None,
    initial_cash: float = 10_000.0,
    margin_per_trade: float = 100.0,
    leverage: float = 5.0,
    fee_rate: float = 0.001,
    slippage_rate: float = 0.0005,
    funding_rate_8h: float = 0.0,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trailing_stop: float | None = None,
    max_hold_bars: int | None = None,
    edge_filter: bool = False,
    min_edge_count: int = 10,
    min_edge_value: float = 0.0,
    bars_per_year: float = 365 * 24,
) -> LabRun:
    """Run a signal strategy with futures-style trade ledger accounting."""
    if prices.empty or len(prices) < 2:
        raise ValueError("need at least two bars")

    symbols = list(prices.columns)
    cash = float(initial_cash)
    reserved_margin = 0.0
    positions: Dict[str, _OpenPosition] = {}
    trades: List[LabTrade] = []
    equity_curve: List[float] = []
    cash_curve: List[float] = []
    timestamps: List[datetime] = []
    fees_paid = 0.0
    funding_paid_total = 0.0
    turnover = 0.0
    skipped_signals = 0
    edge_ledger: Dict[Tuple[str, str], List[float]] = {}
    next_trade_id = 1

    for i in range(len(prices) - 1):
        bar = prices.iloc[i]
        next_bar = prices.iloc[i + 1]
        timestamp = prices.index[i].to_pydatetime()
        next_timestamp = prices.index[i + 1].to_pydatetime()

        # Snapshot before decisions
        unrealized = sum(
            pos.direction * pos.qty * (bar[pos.symbol] - pos.entry_price)
            for pos in positions.values()
        )
        equity = cash + reserved_margin + unrealized
        equity_curve.append(equity)
        cash_curve.append(cash)
        timestamps.append(timestamp)

        # Apply funding to open positions at funding intervals
        if funding_rate_8h != 0.0:
            for pos in positions.values():
                cash = _apply_funding(pos, timestamp, funding_rate_8h, cash)
                funding_paid_total += pos.notional * funding_rate_8h * pos.direction

        # 1. Generate signals using bars up to and including i (no lookahead)
        raw_signals = strategy(prices.iloc[: i + 1])
        signals = []
        for sig in raw_signals:
            if sig.symbol not in symbols:
                continue
            if sig.side != 1:
                continue
            if sig.symbol in positions:
                continue  # one position per symbol at a time
            if cash < margin_per_trade:
                skipped_signals += 1
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
            new_meta = dict(sig.meta)
            new_meta["regime"] = regime
            signals.append(Signal(
                timestamp=sig.timestamp,
                symbol=sig.symbol,
                side=sig.side,
                reason=sig.reason,
                size_fraction=sig.size_fraction,
                meta=new_meta,
            ))

        wanted: Dict[str, int] = {sig.symbol: sig.side for sig in signals}

        # 2. Risk exits on current bar close
        to_close: List[Tuple[str, str, float]] = []
        for sym, pos in list(positions.items()):
            price = bar[sym]
            pos.high_water = max(pos.high_water, price)
            pos.low_water = min(pos.low_water, price)

            pnl_pct = (price - pos.entry_price) / pos.entry_price
            exit_reason = None
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

        # Execute risk exits
        for sym, reason, exit_price in to_close:
            if sym not in positions:
                continue
            pos = positions.pop(sym)
            t = _close_trade(pos, exit_price, next_timestamp, i + 1, fee_rate, slippage_rate, reason)
            trades.append(t)
            reserved_margin -= t.margin
            cash += t.margin + t.net_pnl
            fees_paid += t.fees
            funding_paid_total += t.funding_paid
            turnover += t.notional + (t.qty * t.exit_price)
            edge_ledger.setdefault((t.entry_reason, t.regime), []).append(t.net_pnl)

        # 3. Close positions that are no longer wanted
        for sym, pos in list(positions.items()):
            if wanted.get(sym, 0) != pos.direction:
                pos = positions.pop(sym)
                t = _close_trade(pos, next_bar[sym], next_timestamp, i + 1, fee_rate, slippage_rate, "signal_reverse")
                trades.append(t)
                reserved_margin -= t.margin
                cash += t.margin + t.net_pnl
                fees_paid += t.fees
                funding_paid_total += t.funding_paid
                turnover += t.notional + (t.qty * t.exit_price)
                edge_ledger.setdefault((t.entry_reason, t.regime), []).append(t.net_pnl)

        # 4. Open new positions on t+1 close
        for sig in signals:
            if sig.symbol in positions:
                continue
            if cash < margin_per_trade:
                skipped_signals += 1
                continue
            exec_price = _entry_exec_price(next_bar[sig.symbol], slippage_rate)
            notional = margin_per_trade * leverage
            qty = notional / exec_price
            if qty <= 0:
                continue
            entry_fee = notional * fee_rate
            if cash < margin_per_trade + entry_fee:
                skipped_signals += 1
                continue
            cash -= margin_per_trade + entry_fee
            reserved_margin += margin_per_trade
            positions[sig.symbol] = _OpenPosition(
                trade_id=next_trade_id,
                symbol=sig.symbol,
                direction=1,
                entry_price=exec_price,
                qty=qty,
                margin=margin_per_trade,
                leverage=leverage,
                notional=notional,
                entry_time=next_timestamp,
                entry_bar=i + 1,
                entry_reason=sig.reason,
                regime=sig.meta.get("regime", "unknown"),
                high_water=exec_price,
                low_water=exec_price,
                funding_paid=0.0,
                meta=dict(sig.meta),
            )
            next_trade_id += 1
            fees_paid += entry_fee
            turnover += notional

    # Final mark and close all positions
    final_bar = prices.iloc[-1]
    final_time = prices.index[-1].to_pydatetime()
    for sym, pos in list(positions.items()):
        t = _close_trade(pos, final_bar[sym], final_time, len(prices) - 1, fee_rate, slippage_rate, "end_of_test")
        trades.append(t)
        reserved_margin -= t.margin
        cash += t.margin + t.net_pnl
        fees_paid += t.fees
        funding_paid_total += t.funding_paid
        turnover += t.notional + (t.qty * t.exit_price)
        edge_ledger.setdefault((t.entry_reason, t.regime), []).append(t.net_pnl)

    final_unrealized = sum(
        pos.direction * pos.qty * (final_bar[pos.symbol] - pos.entry_price)
        for pos in positions.values()
    )
    final_equity = cash + reserved_margin + final_unrealized
    equity_curve.append(final_equity)
    cash_curve.append(cash)
    timestamps.append(final_time)

    return LabRun(
        strategy=strategy.__name__,
        trades=trades,
        equity_curve=equity_curve,
        cash_curve=cash_curve,
        timestamps=timestamps,
        fees_paid=fees_paid,
        funding_paid=funding_paid_total,
        turnover=turnover,
        initial_cash=initial_cash,
        final_equity=final_equity,
        reserved_margin=reserved_margin,
        skipped_signals=skipped_signals,
    )
