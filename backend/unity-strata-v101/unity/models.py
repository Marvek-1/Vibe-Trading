from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise ValueError("invalid candle")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("inconsistent OHLC")


@dataclass(frozen=True)
class FundingVenue:
    venue: str
    mark_price: float
    funding_rate: float
    interval_hours: float
    taker_fee: float
    slippage: float = 0.0


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    candles: tuple[Candle, ...]
    bid: float
    ask: float
    bid_depth: float
    ask_depth: float
    funding: tuple[FundingVenue, ...] = ()
    latency_ms: float = 0.0


@dataclass(frozen=True)
class PortfolioState:
    equity: float
    daily_start_equity: float
    peak_equity: float
    open_notional: float = 0.0
    position_notional: float = 0.0
    side: Side = Side.FLAT
    consecutive_losses: int = 0


@dataclass(frozen=True)
class OrderIntent:
    venue: str | None
    side: Side
    notional: float
    order_type: str
    price: float | None = None
    reduce_only: bool = False


@dataclass(frozen=True)
class Action:
    lane: str
    orders: tuple[OrderIntent, ...] = ()
    confidence: float = 0.0
    stop_price: float | None = None
    take_profit_price: float | None = None
    reason: str = ""
    diagnostics: dict[str, float | str] = field(default_factory=dict)

