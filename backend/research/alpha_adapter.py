"""Adapter: zoo alpha factor -> research backtest strategy.

The registry alphas produce a wide DataFrame of factor values. This module
wraps an alpha so it fits the backtest ``Strategy`` contract: given the close
matrix seen so far, return target long-only weights that sum to <= 1.0.
"""

from __future__ import annotations

import logging
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

Strategy = Callable[[pd.DataFrame], dict[str, float]]


def alpha_to_strategy(
    alpha_id: str,
    panel: dict[str, pd.DataFrame],
    top_n: int = 20,
) -> Strategy:
    """Return a ``Strategy`` that selects the top-N assets by ``alpha_id``.

    The alpha is computed once on the full OHLCV panel. At each rebalance bar
    the strategy looks up the pre-computed factor row for that timestamp and
    returns equal-weight targets for the ``top_n`` highest-valued assets.
    """
    from src.factors.registry import RegistryError, SkipAlpha, get_default_registry

    if top_n <= 0:
        raise ValueError("top_n must be greater than zero")
    registry = get_default_registry()
    try:
        factor_df = registry.compute(alpha_id, panel)
    except (SkipAlpha, RegistryError) as exc:
        raise ValueError(f"alpha {alpha_id!r} cannot be computed: {exc}") from exc
    if not isinstance(factor_df, pd.DataFrame):
        raise ValueError(f"alpha {alpha_id!r} returned {type(factor_df).__name__}, expected DataFrame")
    if factor_df.index.has_duplicates:
        factor_df = factor_df[~factor_df.index.duplicated(keep="last")]

    def strategy(prices: pd.DataFrame) -> dict[str, float]:
        if prices.empty:
            return {}
        t = prices.index[-1]
        if t not in factor_df.index:
            return {}
        active = set(prices.columns)
        factors = pd.to_numeric(factor_df.loc[t], errors="coerce")
        factors = factors.replace([float("inf"), float("-inf")], float("nan")).dropna()
        factors = factors[factors.index.isin(active)]
        if factors.empty:
            return {}
        ranked = factors.sort_values(ascending=False, kind="mergesort")
        chosen = ranked.head(top_n).index.tolist()
        if not chosen:
            return {}
        weight = 1.0 / len(chosen)
        return {s: weight for s in chosen}

    return strategy
