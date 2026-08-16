#!/usr/bin/env python3
"""tradingview_mcp — TradingView market intelligence + append-only trade journal.

MoStar Industries / African Flame Initiative
Author: The Flame Architect

Doctrine:
  - The journal is APPEND-ONLY. There are no update or delete tools. A written
    event is a permanent receipt. Corrections are new NOTE events, never edits.
  - Four event types: SIGNAL, ENTRY, EXIT, NOTE.
  - Stats are computed live from the journal; nothing is pre-credited.
    Gate accounting starts at zero.
  - Every event row carries attested_by; attested_by != origin_model applies.
"""

import json
import os
import sqlite3
import time
import uuid
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP
from tradingview_ta import TA_Handler, Interval

mcp = FastMCP("tradingview_mcp")

# --------------------------------------------------------------------------
# Storage — SQLite, append-only journal
# --------------------------------------------------------------------------

DB_PATH = os.environ.get(
    "TV_MCP_DB",
    os.path.join(os.path.expanduser("~"), ".tradingview_mcp", "journal.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id          TEXT PRIMARY KEY,
    ts_utc      REAL NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN ('SIGNAL','ENTRY','EXIT','NOTE')),
    symbol      TEXT NOT NULL,
    exchange    TEXT,
    side        TEXT CHECK (side IN ('LONG','SHORT') OR side IS NULL),
    price       REAL,
    qty         REAL,
    ref_id      TEXT,            -- links EXIT/NOTE back to an ENTRY/SIGNAL id
    detail      TEXT,            -- free text / JSON blob
    attested_by TEXT NOT NULL DEFAULT 'unattested'
);
CREATE INDEX IF NOT EXISTS idx_journal_symbol ON journal(symbol);
CREATE INDEX IF NOT EXISTS idx_journal_type   ON journal(event_type);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT NOT NULL,
    exchange TEXT NOT NULL,
    screener TEXT NOT NULL,
    added_ts REAL NOT NULL,
    PRIMARY KEY (symbol, exchange)
);
"""


def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)  # loud, unconditional, idempotent
    return conn


def _ok(payload) -> str:
    return json.dumps({"ok": True, "data": payload}, default=str)


def _err(msg: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "error": msg, "hint": hint})


# --------------------------------------------------------------------------
# TradingView data tools
# --------------------------------------------------------------------------

_INTERVALS = {
    "1m": Interval.INTERVAL_1_MINUTE, "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES, "30m": Interval.INTERVAL_30_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR, "2h": Interval.INTERVAL_2_HOURS,
    "4h": Interval.INTERVAL_4_HOURS, "1d": Interval.INTERVAL_1_DAY,
    "1W": Interval.INTERVAL_1_WEEK, "1M": Interval.INTERVAL_1_MONTH,
}


class AnalysisInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbol: str = Field(..., description="Ticker, e.g. 'BTCUSDT', 'AAPL'", min_length=1, max_length=20)
    exchange: str = Field("BINANCE", description="Exchange, e.g. 'BINANCE', 'NASDAQ', 'NSENG'")
    screener: str = Field("crypto", description="'crypto', 'america', 'forex', etc.")
    interval: str = Field("1h", description=f"One of: {', '.join(_INTERVALS)}")


def _analyze(symbol: str, exchange: str, screener: str, interval: str) -> dict:
    handler = TA_Handler(
        symbol=symbol.upper(), exchange=exchange.upper(),
        screener=screener.lower(), interval=_INTERVALS[interval],
    )
    a = handler.get_analysis()
    ind = a.indicators
    return {
        "symbol": f"{exchange.upper()}:{symbol.upper()}",
        "interval": interval,
        "recommendation": a.summary["RECOMMENDATION"],
        "counts": {"buy": a.summary["BUY"], "sell": a.summary["SELL"], "neutral": a.summary["NEUTRAL"]},
        "price": {"close": ind.get("close"), "open": ind.get("open"),
                  "high": ind.get("high"), "low": ind.get("low"),
                  "change_pct": ind.get("change"), "volume": ind.get("volume")},
        "indicators": {"RSI": ind.get("RSI"), "MACD": ind.get("MACD.macd"),
                       "MACD_signal": ind.get("MACD.signal"), "ADX": ind.get("ADX"),
                       "EMA20": ind.get("EMA20"), "EMA50": ind.get("EMA50"),
                       "EMA200": ind.get("EMA200"), "ATR": ind.get("ATR"),
                       "BB_upper": ind.get("BB.upper"), "BB_lower": ind.get("BB.lower")},
        "oscillators": a.oscillators["RECOMMENDATION"],
        "moving_averages": a.moving_averages["RECOMMENDATION"],
    }


@mcp.tool(name="tv_get_analysis", annotations={
    "title": "TradingView Analysis", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def tv_get_analysis(params: AnalysisInput) -> str:
    """Fetch TradingView technical analysis for one symbol on one interval.

    Returns recommendation (STRONG_BUY..STRONG_SELL), price snapshot, and key
    indicators (RSI, MACD, ADX, EMAs, ATR, Bollinger).
    """
    if params.interval not in _INTERVALS:
        return _err(f"Unknown interval '{params.interval}'", f"Use one of: {', '.join(_INTERVALS)}")
    try:
        return _ok(_analyze(params.symbol, params.exchange, params.screener, params.interval))
    except Exception as e:
        return _err(str(e), "Check symbol/exchange/screener combination, e.g. BTCUSDT/BINANCE/crypto or AAPL/NASDAQ/america.")


class MultiTFInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbol: str = Field(..., min_length=1, max_length=20)
    exchange: str = Field("BINANCE")
    screener: str = Field("crypto")
    intervals: List[str] = Field(default=["15m", "1h", "4h", "1d"], max_length=6,
                                 description="Timeframes to sweep")


@mcp.tool(name="tv_multi_timeframe", annotations={
    "title": "Multi-Timeframe Sweep", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def tv_multi_timeframe(params: MultiTFInput) -> str:
    """Sweep one symbol across multiple timeframes; returns per-TF recommendation,
    close, RSI, and ADX for confluence reading."""
    out, errors = [], []
    for iv in params.intervals:
        if iv not in _INTERVALS:
            errors.append(f"skipped unknown interval '{iv}'")
            continue
        try:
            a = _analyze(params.symbol, params.exchange, params.screener, iv)
            out.append({"interval": iv, "recommendation": a["recommendation"],
                        "close": a["price"]["close"],
                        "RSI": a["indicators"]["RSI"], "ADX": a["indicators"]["ADX"]})
        except Exception as e:
            errors.append(f"{iv}: {e}")
    return _ok({"symbol": params.symbol.upper(), "sweep": out, "errors": errors})


# --------------------------------------------------------------------------
# Journal tools — APPEND ONLY
# --------------------------------------------------------------------------

class EventType(str, Enum):
    SIGNAL = "SIGNAL"
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    NOTE = "NOTE"


class AppendInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    event_type: EventType = Field(..., description="SIGNAL | ENTRY | EXIT | NOTE")
    symbol: str = Field(..., min_length=1, max_length=20)
    exchange: Optional[str] = Field(None)
    side: Optional[str] = Field(None, description="'LONG' or 'SHORT'", pattern="^(LONG|SHORT)$")
    price: Optional[float] = Field(None, gt=0)
    qty: Optional[float] = Field(None, gt=0)
    ref_id: Optional[str] = Field(None, description="id of the ENTRY/SIGNAL this event resolves or annotates")
    detail: Optional[str] = Field(None, max_length=4000, description="Free text or JSON context")
    attested_by: str = Field("unattested", description="Who witnessed this, e.g. 'flame_manual', 'scanner_v2'. Never leave implicit.")


@mcp.tool(name="journal_append", annotations={
    "title": "Append Journal Event", "readOnlyHint": False,
    "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
async def journal_append(params: AppendInput) -> str:
    """Append one immutable event to the trade journal. There is no edit and no
    delete — corrections are appended as NOTE events referencing the original id.
    EXIT events should carry ref_id of their ENTRY so P/L can be resolved."""
    event_id = str(uuid.uuid4())
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO journal (id, ts_utc, event_type, symbol, exchange, side, price, qty, ref_id, detail, attested_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, time.time(), params.event_type.value, params.symbol.upper(),
             (params.exchange or "").upper() or None, params.side, params.price,
             params.qty, params.ref_id, params.detail, params.attested_by))
        conn.commit()
    finally:
        conn.close()
    return _ok({"id": event_id, "event_type": params.event_type.value,
                "symbol": params.symbol.upper(), "sealed": True})


class ListInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbol: Optional[str] = Field(None, description="Filter by symbol")
    event_type: Optional[EventType] = Field(None)
    limit: int = Field(50, ge=1, le=500)


@mcp.tool(name="journal_list", annotations={
    "title": "List Journal Events", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def journal_list(params: ListInput) -> str:
    """List journal events, newest first, with optional symbol/type filters."""
    q, args = "SELECT * FROM journal WHERE 1=1", []
    if params.symbol:
        q += " AND symbol = ?"; args.append(params.symbol.upper())
    if params.event_type:
        q += " AND event_type = ?"; args.append(params.event_type.value)
    q += " ORDER BY ts_utc DESC LIMIT ?"; args.append(params.limit)
    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    finally:
        conn.close()
    return _ok({"count": len(rows), "events": rows})


class StatsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbol: Optional[str] = Field(None, description="Restrict stats to one symbol")


@mcp.tool(name="journal_stats", annotations={
    "title": "Journal Stats (PF, win rate)", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def journal_stats(params: StatsInput) -> str:
    """Compute live stats from resolved trades (ENTRY paired with EXIT via ref_id):
    resolved count, wins, losses, gross profit/loss, profit factor, win rate.
    Nothing is pre-credited — an unresolved ENTRY contributes zero."""
    conn = _db()
    try:
        sym_clause, args = ("AND e.symbol = ?", [params.symbol.upper()]) if params.symbol else ("", [])
        pairs = conn.execute(f"""
            SELECT e.side, e.price AS entry_px, x.price AS exit_px,
                   COALESCE(e.qty, 1.0) AS qty
            FROM journal e JOIN journal x ON x.ref_id = e.id
            WHERE e.event_type='ENTRY' AND x.event_type='EXIT'
              AND e.price IS NOT NULL AND x.price IS NOT NULL {sym_clause}
        """, args).fetchall()
        totals = conn.execute(
            "SELECT event_type, COUNT(*) c FROM journal " +
            ("WHERE symbol = ? " if params.symbol else "") + "GROUP BY event_type",
            [params.symbol.upper()] if params.symbol else []).fetchall()
    finally:
        conn.close()

    gross_profit = gross_loss = wins = losses = 0.0
    for p in pairs:
        direction = -1.0 if p["side"] == "SHORT" else 1.0
        pnl = (p["exit_px"] - p["entry_px"]) * direction * p["qty"]
        if pnl >= 0:
            wins += 1; gross_profit += pnl
        else:
            losses += 1; gross_loss += -pnl
    resolved = int(wins + losses)
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    return _ok({
        "scope": params.symbol.upper() if params.symbol else "ALL",
        "event_counts": {r["event_type"]: r["c"] for r in totals},
        "resolved_trades": resolved,
        "wins": int(wins), "losses": int(losses),
        "win_rate": round(wins / resolved, 4) if resolved else 0.0,
        "gross_profit": round(gross_profit, 8), "gross_loss": round(gross_loss, 8),
        "profit_factor": (round(pf, 4) if pf != float("inf") else "inf"),
    })


# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------

class WatchAddInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbol: str = Field(..., min_length=1, max_length=20)
    exchange: str = Field("BINANCE")
    screener: str = Field("crypto")


@mcp.tool(name="watchlist_add", annotations={
    "title": "Add to Watchlist", "readOnlyHint": False,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def watchlist_add(params: WatchAddInput) -> str:
    """Add a symbol to the tracked watchlist (idempotent)."""
    conn = _db()
    try:
        conn.execute("INSERT OR IGNORE INTO watchlist (symbol, exchange, screener, added_ts) VALUES (?,?,?,?)",
                     (params.symbol.upper(), params.exchange.upper(), params.screener.lower(), time.time()))
        conn.commit()
    finally:
        conn.close()
    return _ok({"symbol": params.symbol.upper(), "exchange": params.exchange.upper()})


@mcp.tool(name="watchlist_scan", annotations={
    "title": "Scan Watchlist", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def watchlist_scan() -> str:
    """Pull a live 1h TradingView analysis for every symbol on the watchlist."""
    conn = _db()
    try:
        rows = conn.execute("SELECT symbol, exchange, screener FROM watchlist ORDER BY symbol").fetchall()
    finally:
        conn.close()
    results, errors = [], []
    for r in rows:
        try:
            a = _analyze(r["symbol"], r["exchange"], r["screener"], "1h")
            results.append({"symbol": a["symbol"], "recommendation": a["recommendation"],
                            "close": a["price"]["close"], "RSI": a["indicators"]["RSI"]})
        except Exception as e:
            errors.append(f"{r['symbol']}: {e}")
    return _ok({"count": len(results), "watchlist": results, "errors": errors})


if __name__ == "__main__":
    mcp.run(transport="stdio")
