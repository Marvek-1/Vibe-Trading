"""Candidate strategy signal generators for the Profit Lab.

Each strategy must answer the covenant: why was this position entered?
"""

from __future__ import annotations

from typing import List, Dict

import pandas as pd

from profit_lab.models import Signal


def momentum_continuation(
    prices: pd.DataFrame,
    lookback: int = 12,
    top_k: int = 3,
    fee_aware: bool = False,
) -> List[Signal]:
    """Long the top k performers over `lookback` bars; exit if they fall out."""
    if len(prices) < lookback + 1:
        return []
    rets = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
    ranked = rets.sort_values(ascending=False)
    selected = ranked.index[:top_k].tolist()
    signals = []
    for sym in selected:
        if ranked[sym] > 0:
            signals.append(
                Signal(
                    timestamp=prices.index[-1].to_pydatetime(),
                    symbol=sym,
                    side=1,
                    reason=f"momentum_l{lookback}_top{top_k}",
                    size_fraction=1.0 / top_k,
                    meta={"momentum": round(ranked[sym], 6)},
                )
            )
    return signals


def trend_following(
    prices: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    top_k: int = 3,
) -> List[Signal]:
    """Long symbols whose fast MA is above slow MA and whose return is positive."""
    if len(prices) < slow + 1:
        return []
    fast_ma = prices.iloc[-fast:].mean()
    slow_ma = prices.iloc[-slow:].mean()
    trend_up = fast_ma > slow_ma
    rets = prices.iloc[-1] / prices.iloc[-fast] - 1.0
    candidates = [s for s in prices.columns if trend_up.get(s, False) and rets.get(s, 0) > 0]
    selected = sorted(candidates, key=lambda s: rets[s], reverse=True)[:top_k]
    signals = []
    for sym in selected:
        signals.append(
            Signal(
                timestamp=prices.index[-1].to_pydatetime(),
                symbol=sym,
                side=1,
                reason=f"trend_ma{fast}_vs{slow}",
                size_fraction=1.0 / top_k,
                meta={"fast_ma": fast_ma[sym], "slow_ma": slow_ma[sym]},
            )
        )
    return signals


def mean_reversion(
    prices: pd.DataFrame,
    lookback: int = 20,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
) -> List[Signal]:
    """Long symbols whose z-score of recent return is below -entry_z."""
    if len(prices) < lookback + 1:
        return []
    mean = prices.pct_change().iloc[-lookback:].mean()
    std = prices.pct_change().iloc[-lookback:].std()
    last_ret = prices.iloc[-1] / prices.iloc[-2] - 1.0
    z = ((last_ret - mean) / std).replace([float("inf"), -float("inf"), float("nan")], 0.0)
    signals = []
    for sym in prices.columns:
        if z[sym] < -entry_z:
            signals.append(
                Signal(
                    timestamp=prices.index[-1].to_pydatetime(),
                    symbol=sym,
                    side=1,
                    reason=f"mean_rev_z{entry_z}_l{lookback}",
                    size_fraction=1.0 / len(prices.columns),
                    meta={"z_score": round(z[sym], 4)},
                )
            )
    return signals


def breakout(
    prices: pd.DataFrame,
    lookback: int = 20,
    top_k: int = 3,
) -> List[Signal]:
    """Long symbols making a new `lookback`-bar high with positive volume placeholder."""
    if len(prices) < lookback + 1:
        return []
    highest = prices.iloc[-lookback:-1].max()
    current = prices.iloc[-1]
    breakout_mask = current > highest
    rets = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
    candidates = [s for s in prices.columns if breakout_mask.get(s, False) and rets.get(s, 0) > 0]
    selected = sorted(candidates, key=lambda s: rets[s], reverse=True)[:top_k]
    signals = []
    for sym in selected:
        signals.append(
            Signal(
                timestamp=prices.index[-1].to_pydatetime(),
                symbol=sym,
                side=1,
                reason=f"breakout_l{lookback}",
                size_fraction=1.0 / top_k,
                meta={"breakout_pct": round(rets[sym], 4)},
            )
        )
    return signals
