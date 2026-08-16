"""Immutable local OHLCV store fed from Binance klines via ccxt.

Bars are stored one CSV per symbol/timeframe under ``research/data``. Files
are append-only from the caller's perspective: every write merges new bars
with what is on disk, dedupes on timestamp, sorts, and rewrites atomically.
Historical bars already stored are never altered by a refresh.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
MAX_BARS_PER_REQUEST = 1000


def bars_path(symbol: str, timeframe: str, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / f"{symbol.upper()}_{timeframe}.csv"


def load_bars(symbol: str, timeframe: str, data_dir: Path = DATA_DIR) -> pd.DataFrame:
    path = bars_path(symbol, timeframe, data_dir)
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.read_csv(path)
    frame["ts"] = frame["ts"].astype("int64")
    return frame[COLUMNS]


def save_bars(symbol: str, timeframe: str, new_bars: pd.DataFrame, data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """Merge new bars into the store; existing rows win on timestamp collisions."""
    existing = load_bars(symbol, timeframe, data_dir)
    incoming = new_bars[COLUMNS].copy()
    incoming["ts"] = incoming["ts"].astype("int64")
    if existing.empty:
        merged = incoming
    else:
        merged = pd.concat([existing, incoming], ignore_index=True)
    merged = merged.drop_duplicates(subset="ts", keep="first").sort_values("ts").reset_index(drop=True)

    path = bars_path(symbol, timeframe, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    merged.to_csv(tmp, index=False)
    tmp.replace(path)
    return merged


def _get_exchange(market_type: str = "spot"):
    import ccxt

    common = {"enableRateLimit": True, "timeout": 15_000}
    if market_type == "swap":
        return ccxt.binanceusdm(common)
    return ccxt.binance(common)


def _ccxt_symbol(code: str, market_type: str = "spot") -> str:
    base_quote = code.replace("-", "/").upper()
    if market_type == "swap":
        return f"{base_quote}:USDT"
    return base_quote


def download_history(
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int | None = None,
    data_dir: Path = DATA_DIR,
    exchange=None,
    sleep_seconds: float = 0.2,
    market_type: str = "spot",
) -> pd.DataFrame:
    """Page through Binance klines from ``since_ms`` and persist them.

    Resumable: reruns only fetch missing older or newer bars; existing rows win on collisions.
    """
    exchange = exchange if exchange is not None else _get_exchange(market_type=market_type)
    existing = load_bars(symbol, timeframe, data_dir)
    ccxt_symbol = _ccxt_symbol(symbol, market_type=market_type)

    def _page(start_ms: int, stop_ms: int | None) -> list[list[float]]:
        out: list[list[float]] = []
        cursor = start_ms
        while True:
            if stop_ms is not None and cursor >= stop_ms:
                break
            batch = exchange.fetch_ohlcv(ccxt_symbol, timeframe, since=cursor, limit=MAX_BARS_PER_REQUEST)
            if not batch:
                break
            out.extend(batch)
            last_ts = int(batch[-1][0])
            if last_ts + 1 <= cursor:
                break
            cursor = last_ts + 1
            if len(batch) < MAX_BARS_PER_REQUEST:
                break
            if stop_ms is not None and last_ts >= stop_ms:
                break
            time.sleep(sleep_seconds)
        return out

    rows: list[list[float]] = []
    if not existing.empty:
        min_ts = int(existing["ts"].min())
        max_ts = int(existing["ts"].max())
        if since_ms < min_ts:
            rows.extend(_page(since_ms, min_ts))
        rows.extend(_page(max_ts + 1, until_ms))
    else:
        rows.extend(_page(since_ms, until_ms))

    if rows:
        new_bars = pd.DataFrame(rows, columns=COLUMNS)
        new_bars["ts"] = new_bars["ts"].astype("int64")
        if until_ms is not None:
            new_bars = new_bars[new_bars["ts"] < until_ms]
        return save_bars(symbol, timeframe, new_bars, data_dir)
    return existing


def load_close_matrix(symbols: list[str], timeframe: str, data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """Aligned close-price matrix (rows: shared timestamps, columns: symbols).

    Uses the intersection of timestamps so every backtest bar has a price for
    every symbol -- no forward-filling that would fabricate tradable quotes.
    """
    closes: dict[str, pd.Series] = {}
    for symbol in symbols:
        bars = load_bars(symbol, timeframe, data_dir)
        if bars.empty:
            raise ValueError(f"no stored bars for {symbol} {timeframe}; run download_history first")
        closes[symbol] = bars.set_index("ts")["close"]
    matrix = pd.DataFrame(closes).dropna().sort_index()
    if matrix.empty:
        raise ValueError("no overlapping timestamps across requested symbols")
    return matrix
