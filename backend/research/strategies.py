"""Baseline and candidate strategies for the walk-forward gauntlet.

Every strategy follows the same contract: given a close-price matrix
(rows: timestamps, columns: symbols) up to the current bar, return a
dict mapping symbol to target portfolio weight. Positive weights only;
shorts and leverage are intentionally excluded from this scaffold.
"""

from __future__ import annotations

import random
from functools import partial
from typing import Callable

import pandas as pd

Strategy = Callable[[pd.DataFrame], dict[str, float]]


def equal_weight(prices: pd.DataFrame) -> dict[str, float]:
    symbols = list(prices.columns)
    return {s: 1.0 / len(symbols) for s in symbols}


def buy_and_hold(prices: pd.DataFrame) -> dict[str, float]:
    if len(prices) == 1:
        return equal_weight(prices)
    return {}


def random_weight(seed: int = 0) -> Strategy:
    def strategy(prices: pd.DataFrame) -> dict[str, float]:
        index_value = prices.index[-1]
        bar_seed = int(index_value.value) if isinstance(index_value, pd.Timestamp) else int(index_value)
        rng = random.Random(int(seed) + bar_seed)
        symbols = list(prices.columns)
        weights = [rng.random() for _ in symbols]
        total = sum(weights)
        return {s: w / total for s, w in zip(symbols, weights)}
    return strategy


def ts_momentum(
    lookback: int = 12,
    fee_aware: bool = False,
    target_volatility: float | None = None,
    min_history: int = 2,
) -> Strategy:
    def strategy(prices: pd.DataFrame) -> dict[str, float]:
        if len(prices) < max(lookback, min_history):
            return equal_weight(prices)
        rets = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
        longs = (rets > 0).astype(float)
        total = longs.sum()
        if total == 0:
            return {}
        weights = (longs / total).to_dict()
        if target_volatility is not None and len(prices) > lookback + 1:
            vol = prices.pct_change().iloc[-lookback:].std()
            for s, w in list(weights.items()):
                if vol.get(s, 0) > 0:
                    weights[s] = w * (target_volatility / vol[s])
            cap = sum(weights.values())
            if cap > 0:
                weights = {s: w / cap for s, w in weights.items()}
        return weights
    return strategy


def cs_momentum(
    lookback: int = 12,
    top_k: int = 3,
    min_history: int = 2,
) -> Strategy:
    def strategy(prices: pd.DataFrame) -> dict[str, float]:
        if len(prices) < max(lookback, min_history):
            return equal_weight(prices)
        rets = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
        ranked = rets.sort_values(ascending=False)
        selected = ranked.index[:top_k].tolist()
        if not selected:
            return {}
        return {s: 1.0 / len(selected) for s in selected}
    return strategy


def ts_zscore_reversal(
    lookback: int = 20,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
) -> Strategy:
    """Long-only time-series mean reversion: per-symbol z-score of its own
    lookback-bar return, entering when a symbol is statistically oversold
    relative to its own recent history and holding until it partially
    reverts. No cross-sectional ranking -- each symbol's entry/exit is
    independent of every other symbol's state.

    Shorts are intentionally excluded (this module's contract is long-only,
    see the module docstring); a symbol with z > entry_z is simply not
    traded rather than shorted.

    State (which symbols are currently held) persists across calls within
    one contiguous price window, but resets whenever the incoming window's
    start timestamp changes -- this keeps each walk-forward split's
    validate/test evaluation independent instead of leaking position state
    from one split into the next.
    """
    state: dict[str, int] = {}
    window_start: object = None

    def strategy(prices: pd.DataFrame) -> dict[str, float]:
        nonlocal window_start
        if len(prices) < 2 * lookback:
            return {}
        current_start = prices.index[0]
        if current_start != window_start:
            window_start = current_start
            state.clear()

        returns = prices.pct_change(lookback)
        mean_ret = returns.rolling(lookback).mean()
        std_ret = returns.rolling(lookback).std()
        z = (returns - mean_ret) / std_ret
        curr_z = z.iloc[-1]

        for symbol in prices.columns:
            zs = curr_z.get(symbol)
            if pd.isna(zs):
                continue
            held = state.get(symbol, 0)
            if held == 0 and zs < -entry_z:
                state[symbol] = 1
            elif held == 1 and zs > -exit_z:
                state[symbol] = 0

        active = [s for s in prices.columns if state.get(s, 0) == 1]
        if not active:
            return {}
        weight = 1.0 / len(active)
        return {s: weight for s in active}

    return strategy


def vwap_capitulation(
    panel: dict[str, pd.DataFrame],
    atr_lookback: int = 14,
    vwap_lookback: int = 24,
    entry_band: float = 1.5,
    time_stop: int = 24,
) -> Strategy:
    """Long-only volume-confirmed mean reversion: buy when close is more than
    ``entry_band`` ATRs below the rolling VWAP *and* volume exceeds its own
    rolling average (capitulation, not a quiet drift below fair value). Exit
    on reversion to VWAP or after ``time_stop`` bars.

    Unlike every other Strategy in this module, this one needs high/low/
    volume, not just close -- the walk-forward Strategy contract only passes
    a close matrix, so VWAP/ATR/volume-MA are precomputed once here from the
    full OHLCV panel (all rolling and backward-looking, no lookahead) and
    looked up by timestamp at call time, the same pattern
    research.alpha_adapter.alpha_to_strategy uses for zoo alphas.

    State (which symbols are held) resets whenever the incoming price
    window's start timestamp changes, matching ts_zscore_reversal's
    per-split-window isolation.
    """
    high = panel["high"]
    low = panel["low"]
    close = panel["close"]
    volume = panel["volume"]

    typical_price = (high + low + close) / 3.0
    vwap = (typical_price * volume).rolling(vwap_lookback).sum() / volume.rolling(vwap_lookback).sum()

    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        keys=["hl", "hc", "lc"],
    ).groupby(level=1).max()
    atr = true_range.rolling(atr_lookback).mean()

    vol_ma = volume.rolling(vwap_lookback).mean()

    signal = (close < vwap - entry_band * atr) & (volume > vol_ma)
    signal = signal.fillna(False)

    window_start: object = None
    state: dict[str, int] = {}

    def strategy(prices: pd.DataFrame) -> dict[str, float]:
        nonlocal window_start
        if len(prices) < 2:
            return {}
        current_start = prices.index[0]
        if current_start != window_start:
            window_start = current_start
            state.clear()

        window_close = close.loc[current_start : prices.index[-1]]
        window_vwap = vwap.reindex(window_close.index)
        window_signal = signal.reindex(window_close.index).fillna(False)

        held: dict[str, int] = {}
        entry_bar: dict[str, int] = {}
        for i, t in enumerate(window_close.index):
            for symbol in window_close.columns:
                cur = held.get(symbol, 0)
                if cur == 0:
                    if bool(window_signal.loc[t, symbol]):
                        held[symbol] = 1
                        entry_bar[symbol] = i
                else:
                    reverted = window_close.loc[t, symbol] >= window_vwap.loc[t, symbol]
                    timed_out = (i - entry_bar.get(symbol, i)) >= time_stop
                    if reverted or timed_out:
                        held[symbol] = 0
        state.clear()
        state.update(held)

        active = [s for s in prices.columns if state.get(s, 0) == 1]
        if not active:
            return {}
        weight = 1.0 / len(active)
        return {s: weight for s in active}

    return strategy


def cost_band_rebalance(band_fraction: float = 0.02) -> Strategy:
    def strategy(prices: pd.DataFrame) -> dict[str, float]:
        return equal_weight(prices)
    return strategy


def regime_momentum(
    prices: pd.DataFrame,
    lookback: int = 24,
    sma_period: int = 50,
    top_n: int = 3,
) -> dict[str, float]:
    """
    Long only the top-N symbols by recent return, but only if they are above
    their SMA (regime filter). All other symbols get a zero weight.
    """
    if len(prices) < max(lookback, sma_period):
        return {symbol: 1.0 / len(prices.columns) for symbol in prices.columns}

    symbols = list(prices.columns)
    recent_return = prices.iloc[-1] / prices.iloc[-lookback] - 1.0

    # Regime filter: price > SMA
    sma = prices.iloc[-sma_period:].mean()
    above_sma = prices.iloc[-1] > sma

    # Only eligible symbols
    eligible = {symbol: recent_return[symbol] for symbol in symbols if above_sma[symbol]}
    if not eligible:
        # No symbols pass the regime filter -> sit in cash
        return {}

    # Sort by return descending, pick top N
    sorted_eligible = sorted(eligible, key=eligible.get, reverse=True)
    chosen = sorted_eligible[:top_n]

    weight = 1.0 / len(chosen)
    return {symbol: weight for symbol in chosen}


def top_gainers_spot(
    prices: pd.DataFrame,
    lookback: int = 24,
    top_n: int = 30,
) -> dict[str, float]:
    """
    Select the top-N symbols by return over the last `lookback` bars,
    then allocate equal weight. If fewer than `top_n` symbols are available,
    only those are taken (rest cash).
    """
    if len(prices) < lookback + 1:
        n = len(prices.columns)
        return {s: 1.0 / n for s in prices.columns}

    recent_returns = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
    sorted_symbols = sorted(
        prices.columns, key=lambda s: recent_returns[s], reverse=True
    )
    chosen = sorted_symbols[:top_n]

    if not chosen:
        return {}

    weight = 1.0 / len(chosen)
    return {s: weight for s in chosen}


def top_gainers_filtered(
    prices: pd.DataFrame,
    lookback: int = 24,
    top_n: int = 30,
    sma_period: int = 50,
) -> dict[str, float]:
    """
    Top-N gainers that are also above their SMA (trend filter).
    """
    if len(prices) < max(lookback, sma_period) + 1:
        n = len(prices.columns)
        return {s: 1.0 / n for s in prices.columns}

    recent_returns = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
    sma = prices.iloc[-sma_period:].mean()
    above_sma = prices.iloc[-1] > sma

    sorted_symbols = sorted(
        [s for s in prices.columns if above_sma[s]],
        key=lambda s: recent_returns[s],
        reverse=True,
    )
    chosen = sorted_symbols[:top_n]

    if not chosen:
        return {}  # all cash

    weight = 1.0 / len(chosen)
    return {s: weight for s in chosen}


def mean_reversion_losers(
    prices: pd.DataFrame,
    lookback: int = 24,
    top_n: int = 10,
) -> dict[str, float]:
    """
    Buy the biggest N losers over the last `lookback` bars, equal weight.
    Hypothesis: extreme selloffs bounce.
    """
    if len(prices) < lookback + 1:
        n = len(prices.columns)
        return {s: 1.0 / n for s in prices.columns}

    recent_returns = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
    sorted_symbols = sorted(
        prices.columns, key=lambda s: recent_returns[s]
    )
    chosen = sorted_symbols[:top_n]

    if not chosen:
        return {}

    weight = 1.0 / len(chosen)
    return {s: weight for s in chosen}


def pullback_reversion(
    prices: pd.DataFrame,
    lookback: int = 24,
    top_n: int = 10,
    sma_period: int = 50,
) -> dict[str, float]:
    """
    Buy the biggest 24h losers that are still above their SMA(50).
    This targets pullbacks within an established uptrend.
    """
    if len(prices) < max(lookback, sma_period) + 1:
        n = len(prices.columns)
        return {s: 1.0 / n for s in prices.columns}

    recent_returns = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
    sma = prices.iloc[-sma_period:].mean()
    above_sma = prices.iloc[-1] > sma

    sorted_symbols = sorted(
        [s for s in prices.columns if above_sma[s]],
        key=lambda s: recent_returns[s],
    )
    chosen = sorted_symbols[:top_n]

    if not chosen:
        return {}  # all cash

    weight = 1.0 / len(chosen)
    return {s: weight for s in chosen}


def pullback_reversion_oversold(
    prices: pd.DataFrame,
    lookback: int = 24,
    rsi_period: int = 14,
    rsi_threshold: float = 30.0,
    top_n: int = 10,
    sma_period: int = 50,
) -> dict[str, float]:
    """
    Buy the biggest lookback losers that are still above their SMA(50)
    AND have RSI < threshold (oversold). This targets pullbacks within
    an uptrend at deeply oversold moments.
    """
    if len(prices) < max(lookback, sma_period, rsi_period) + 1:
        n = len(prices.columns)
        return {s: 1.0 / n for s in prices.columns}

    recent_returns = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
    sma = prices.iloc[-sma_period:].mean()
    above_sma = prices.iloc[-1] > sma

    # RSI
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(window=rsi_period, min_periods=rsi_period).mean()
    avg_loss = loss.rolling(window=rsi_period, min_periods=rsi_period).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - 100.0 / (1.0 + rs)
    current_rsi = rsi.iloc[-1]

    symbols = list(prices.columns)
    eligible = [
        s
        for s in symbols
        if above_sma[s]
        and not pd.isna(current_rsi[s])
        and current_rsi[s] < rsi_threshold
        and not pd.isna(recent_returns[s])
    ]

    sorted_symbols = sorted(eligible, key=lambda s: recent_returns[s])
    chosen = sorted_symbols[:top_n]

    if not chosen:
        return {}  # all cash

    weight = 1.0 / len(chosen)
    return {s: weight for s in chosen}


def top_gainers_filtered_v1(
    prices: pd.DataFrame,
    lookback: int = 24,
    top_n: int = 30,
    sma_period: int = 50,
) -> dict[str, float]:
    """
    Frozen v1 of top_gainers_filtered: top-N gainers above their SMA.
    """
    if len(prices) < max(lookback, sma_period) + 1:
        n = len(prices.columns)
        return {s: 1.0 / n for s in prices.columns}

    recent_returns = prices.iloc[-1] / prices.iloc[-lookback] - 1.0
    sma = prices.iloc[-sma_period:].mean()
    above_sma = prices.iloc[-1] > sma

    sorted_symbols = sorted(
        [s for s in prices.columns if above_sma[s]],
        key=lambda s: recent_returns[s],
        reverse=True,
    )
    chosen = sorted_symbols[:top_n]

    if not chosen:
        return {}  # all cash

    weight = 1.0 / len(chosen)
    return {s: weight for s in chosen}


BASELINES: dict[str, Strategy] = {
    "buy_hold": buy_and_hold,
    "equal_weight": equal_weight,
    "random": random_weight(seed=42),
}

CANDIDATES: dict[str, Strategy] = {
    "ts_mom_12": ts_momentum(lookback=12),
    "ts_mom_24": ts_momentum(lookback=24),
    "cs_mom_12_3": cs_momentum(lookback=12, top_k=3),
    "cs_mom_24_3": cs_momentum(lookback=24, top_k=3),
    "cost_band_2pct": equal_weight,
    "cost_band_5pct": equal_weight,
    "exit_salvage_5pct": equal_weight,
    "exit_salvage_10pct": equal_weight,
    "regime_momentum": regime_momentum,
    "top_gainers_spot": top_gainers_spot,
    "top_gainers_filtered": top_gainers_filtered,
    "top_gainers_filtered_v1": top_gainers_filtered_v1,
    "mean_reversion_losers": mean_reversion_losers,
    "pullback_reversion": pullback_reversion,
}

CANDIDATE_PARAMS: dict[str, dict[str, Any]] = {
    "cost_band_2pct": {"band_fraction": 0.02},
    "cost_band_5pct": {"band_fraction": 0.05},
    "exit_salvage_5pct": {"stop_loss": 0.05},
    "exit_salvage_10pct": {"stop_loss": 0.10},
    "regime_momentum": {},
    "top_gainers_spot": {"take_profit": 0.05, "stop_loss": 0.03},
    "top_gainers_filtered": {"take_profit": 0.05, "stop_loss": 0.03},
    "top_gainers_filtered_v1": {"take_profit": 0.05, "stop_loss": 0.03},
    "mean_reversion_losers": {"take_profit": 0.05, "stop_loss": 0.03},
    "pullback_reversion": {"take_profit": 0.05, "stop_loss": 0.03},
}

# Grid-search variants: pullback in uptrend (SMA50) + oversold RSI filter.
for _lookback in (12, 24, 48):
    for _rsi in (25, 30, 35):
        _name = f"pullback_oversold_{_lookback}_{_rsi}"
        CANDIDATES[_name] = partial(
            pullback_reversion_oversold,
            lookback=_lookback,
            rsi_threshold=_rsi,
        )
        CANDIDATE_PARAMS[_name] = {"take_profit": 0.05, "stop_loss": 0.03}

