"""Profit Lab data models.

Every completed trade is an immutable ledger entry with its own margin, leverage,
funding, and realized P&L. The session wallet is the sum of cash, reserved margin,
and open unrealized P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Signal:
    """A single entry/exit signal produced by a strategy."""

    timestamp: datetime
    symbol: str
    side: int  # 1 for long, -1 for short
    reason: str  # entry reason, e.g. "momentum_12h", "breakout_20bar"
    size_fraction: float  # notional weight hint; engine may override
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LabTrade:
    """One immutable futures trade ledger entry."""

    # Identity
    trade_id: int
    symbol: str
    direction: int  # 1 long, -1 short
    entry_time: datetime
    exit_time: datetime

    # Prices
    entry_price: float
    exit_price: float
    qty: float  # coin quantity = notional / entry_price

    # Margin and leverage (fixed per trade like Binance isolated)
    margin: float
    leverage: float
    notional: float  # margin * leverage

    # Reasons
    entry_reason: str
    exit_reason: str
    regime: str

    # P&L components (all in quote asset, e.g. USDT)
    gross_pnl: float  # (exit - entry) * qty * direction
    entry_fee: float
    exit_fee: float
    fees: float  # entry_fee + exit_fee
    funding_paid: float
    net_pnl: float  # gross_pnl - fees - funding_paid
    net_pnl_pct: float  # net_pnl / margin  (ROI on margin)

    hold_bars: int
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def win(self) -> bool:
        return self.net_pnl > 0


@dataclass(frozen=True)
class LabRun:
    """Result of one lab backtest run."""

    strategy: str
    trades: List[LabTrade]
    equity_curve: List[float]
    cash_curve: List[float]  # free cash
    timestamps: List[datetime]
    fees_paid: float
    funding_paid: float
    turnover: float
    initial_cash: float
    final_equity: float
    reserved_margin: float
    skipped_signals: int = 0
    params: Dict[str, Any] = field(default_factory=dict)
