"""Market regime classification for the Mo Profit Lab.

Regimes are per-symbol, computed from OHLCV using:
- ADX-like trend strength (DM+ / DM- over wilder smoothing)
- ATR percentile (volatility)

Result labels:
  trending_high_vol  | trending_low_vol
  ranging_high_vol   | ranging_low_vol
"""

from __future__ import annotations

from typing import Dict

import pandas as pd


def _true_range(df: pd.DataFrame) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close_prev = df["close"].shift(1)
    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = _true_range(df)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    atr = _atr(df, period)
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def classify_at(df: pd.DataFrame, period: int = 14, vol_lookback: int = 50) -> str:
    """Return the regime label for the last row of a single-symbol OHLCV DataFrame."""
    if len(df) < max(period, vol_lookback) + 1:
        return "unknown"
    adx = _adx(df, period)
    atr = _atr(df, period)
    atr_pct = atr / df["close"]
    # Volatility percentile over recent history
    vol_hist = atr_pct.iloc[-vol_lookback:]
    current_vol = atr_pct.iloc[-1]
    high_vol = current_vol > vol_hist.quantile(0.70)
    low_vol = current_vol < vol_hist.quantile(0.30)
    trend = adx.iloc[-1]
    if trend >= 25:
        return "trending_high_vol" if high_vol else "trending_low_vol"
    return "ranging_high_vol" if high_vol else "ranging_low_vol"


def _classify_series(df: pd.DataFrame, period: int = 14, vol_lookback: int = 50) -> pd.Series:
    """Vectorized per-symbol regime labels."""
    adx = _adx(df, period)
    atr = _atr(df, period)
    atr_pct = atr / df["close"]
    high_thr = atr_pct.rolling(vol_lookback, min_periods=vol_lookback).quantile(0.70)
    low_thr = atr_pct.rolling(vol_lookback, min_periods=vol_lookback).quantile(0.30)
    high_vol = atr_pct > high_thr
    low_vol = atr_pct < low_thr
    trend = adx >= 25
    cond = pd.DataFrame({"trend": trend, "high": high_vol, "low": low_vol})
    labels = pd.Series(index=df.index, dtype=str).fillna("unknown")
    labels[cond["trend"] & cond["high"]] = "trending_high_vol"
    labels[cond["trend"] & ~cond["high"]] = "trending_low_vol"
    labels[~cond["trend"] & cond["high"]] = "ranging_high_vol"
    labels[~cond["trend"] & cond["low"]] = "ranging_low_vol"
    # anything left is just ranging_mid (filler)
    labels[labels == "unknown"] = "ranging_mid_vol"
    labels.iloc[: max(period, vol_lookback)] = "unknown"
    return labels


def build_regime_series(ohlcv: Dict[str, pd.DataFrame], period: int = 14, vol_lookback: int = 50) -> pd.DataFrame:
    """Build a DataFrame (timestamps × symbols) of regime labels."""
    labels = {}
    for sym, df in ohlcv.items():
        labels[sym] = _classify_series(df, period, vol_lookback)
    return pd.concat(labels, axis=1)
