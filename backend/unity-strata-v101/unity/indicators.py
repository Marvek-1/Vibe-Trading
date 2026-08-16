from __future__ import annotations

import math
from collections.abc import Sequence

from .models import Candle


def ema(values: Sequence[float], period: int) -> float:
    if period < 1 or len(values) < period:
        raise ValueError("insufficient values for EMA")
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for item in values[period:]:
        value += alpha * (item - value)
    return value


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        raise ValueError("insufficient candles for ATR")
    ranges = []
    for prev, current in zip(candles[-period - 1:-1], candles[-period:]):
        ranges.append(max(current.high - current.low, abs(current.high - prev.close), abs(current.low - prev.close)))
    return sum(ranges) / period


def realized_volatility(closes: Sequence[float], period: int = 30) -> float:
    if len(closes) < period + 1:
        raise ValueError("insufficient closes for volatility")
    returns = [math.log(b / a) for a, b in zip(closes[-period - 1:-1], closes[-period:])]
    mean = sum(returns) / len(returns)
    return math.sqrt(sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1))


def efficiency_ratio(closes: Sequence[float], period: int = 20) -> float:
    window = closes[-period - 1:]
    if len(window) < period + 1:
        raise ValueError("insufficient closes for efficiency ratio")
    path = sum(abs(b - a) for a, b in zip(window, window[1:]))
    return abs(window[-1] - window[0]) / path if path else 0.0


def zscore(values: Sequence[float], period: int = 30) -> float:
    window = values[-period:]
    if len(window) < period:
        raise ValueError("insufficient values for z-score")
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    return (window[-1] - mean) / math.sqrt(variance) if variance > 0 else 0.0

