"""Simulated live paper-trading session against public futures market data.

This is NOT the real-money live-runner in ``src/live/`` (no broker order is
ever placed, no mandate/halt/order-guard machinery applies here). It is a
synthetic cash+position ledger, marked to market against validated public
exchange data on a schedule, receipted to disk the same way a governed backtest
run is: every entry, every trade, and every mark is a written, hash-verified
record, not narration. Checking P&L means reading the ``.jsonl`` files from
disk, not asking an agent what it remembers.

Strategy is periodic equal-weight rebalancing: enter equal-weight across the
symbol universe at session start, then on a fixed schedule sell whatever has
drifted above its equal-weight share and buy whatever has drifted below it,
snapping back to equal weight. No forecasting, no signal, no fees/slippage
modeled -- a disciplined rebalance schedule, not a predictive strategy.

State is split two ways:
  session.json  -- static config, written once at start (symbols, initial
                   cash, rebalance interval). Never rewritten.
  book.json     -- live, mutable: current positions, cash, last rebalance
                   time. Rewritten every time a rebalance executes.
  marks.jsonl   -- append-only equity snapshots, one per poll.
  trades.jsonl  -- append-only trade log, one entry per executed buy/sell
                   (including the entry trades that open each position).
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import functools
import hashlib
import json
import logging
import math
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from write_receipt import receipted_write  # agent/write_receipt.py, top-level module
from futures_paper_engine import request_json, normalize_symbol
from paper_accounting_guard import assess_accounting, position_ledger_differences
from accounting.futures_ledger import (
    Account as LedgerAccount,
    AccountingInvariantError,
    Position as LedgerPosition,
    Side as LedgerSide,
    apply_slippage as ledger_apply_slippage,
    close_position as ledger_close_position,
    dec as ledger_dec,
    open_position as ledger_open_position,
)

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path(__file__).resolve().parent / "paper_sessions"
STRATEGY_TYPE = "periodic_equal_weight_rebalance"
FUNDING_ZSCORE_STRATEGY = "funding_rate_zscore"


class ConcurrentSessionMutation(RuntimeError):
    """Raised when another process is already mutating the same session."""


def exclusive_session_mutation(function):
    """Serialize a complete ledger mutation across processes.

    A crash releases this advisory lock with its file descriptor, avoiding
    stale PID locks. Holding it across read, append, book write, and invariant
    verification prevents overlapping trade plans from sharing one ledger.
    """

    @functools.wraps(function)
    def guarded(session_dir: Path, *args: Any, **kwargs: Any):
        session_dir = Path(session_dir)
        lock_path = session_dir / ".mutation.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConcurrentSessionMutation(
                    f"session {session_dir.name} already has an active ledger mutation"
                ) from exc
            try:
                return function(session_dir, *args, **kwargs)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    return guarded

# ── Rate limiter for Binance API calls ────────────────────────────────────

class _RateLimiter:
    """Simple token-bucket rate limiter for exchange API calls."""

    def __init__(self, rate: float = 10.0, per: float = 1.0) -> None:
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_refill = time.monotonic()
        self.lock = Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            if elapsed > self.per:
                self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
                self.last_refill = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return
            wait_time = (tokens - self.tokens) / self.rate
            time.sleep(wait_time)
            self.tokens = 0.0
            self.last_refill = time.monotonic()

_binance_limiter = _RateLimiter(rate=10.0)


# ── Receipt hash verification ─────────────────────────────────────────────

def verify_receipted_file(path: Path) -> bool:
    """Check if file matches its .hash sidecar.

    Returns True if no hash sidecar exists (legacy files) or if the hash
    matches. Returns False only on a confirmed mismatch (tampering).
    """
    hash_path = path.with_suffix(path.suffix + ".hash")
    if not hash_path.exists():
        return True
    current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    stored_hash = hash_path.read_text(encoding="utf-8").strip()
    return current_hash == stored_hash


# ── Crash-safe journal ────────────────────────────────────────────────────

def _append_journal(session_dir: Path, entry: dict[str, Any]) -> None:
    """Append a state-change entry to the session journal."""
    journal_path = session_dir / "journal.jsonl"
    with journal_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now_iso(), **entry}, ensure_ascii=False) + "\n")
        f.flush()


def _recover_from_journal(session_dir: Path) -> Optional[dict[str, Any]]:
    """Check journal for incomplete transactions and return last checkpoint.

    If the last journal entry is not 'commit', the previous 'checkpoint'
    state is returned so the caller can restore it. Returns None if the
    journal is clean (last entry is 'commit' or no journal exists).
    """
    journal_path = session_dir / "journal.jsonl"
    if not journal_path.exists():
        return None

    entries = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not entries:
        return None

    last = entries[-1]
    if last.get("type") == "commit":
        return None  # clean state

    # Find the last checkpoint before the incomplete transaction
    for entry in reversed(entries):
        if entry.get("type") == "checkpoint":
            return entry.get("state")
    return None


# ── Heartbeat ─────────────────────────────────────────────────────────────

def _update_heartbeat(session_dir: Path) -> None:
    """Write a .heartbeat file with the current UTC timestamp."""
    hb_path = session_dir / ".heartbeat"
    hb_path.write_text(_now_iso(), encoding="utf-8")


def _check_heartbeat(session_dir: Path, stale_after_seconds: int = 600) -> str:
    """Check heartbeat freshness. Returns 'running' or 'stopped'."""
    hb_path = session_dir / ".heartbeat"
    if not hb_path.exists():
        return "stopped"
    try:
        hb_time = _parse_iso(hb_path.read_text(encoding="utf-8").strip())
        age = datetime.now(timezone.utc) - hb_time
        return "running" if age < timedelta(seconds=stale_after_seconds) else "stopped"
    except Exception:
        return "stopped"

# Bumped whenever the cash/equity accounting model changes shape (e.g. the
# fee-in-cost-basis fix, the reconciliation invariant below) so a session
# written under an old model is never silently read as if it matched the
# current one -- see session_reconciliation.py, which had to reconstruct a
# session where cash_remaining had drifted ~$900 from what trades.jsonl
# implied because it predated this versioning.
ACCOUNTING_SCHEMA_VERSION = 3

# Same tolerance model used by compute_session_diagnostics's `reconciled`
# flag -- kept as one constant so the live halt below and the diagnostics
# check can't silently drift apart.
RECONCILIATION_ABS_TOLERANCE = 1e-6
RECONCILIATION_REL_TOLERANCE = 1e-9

# Rebalance trades below this notional are skipped -- floating-point/price-
# noise dust, not a real drift worth recording as a trade.
MIN_TRADE_NOTIONAL = 1.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The receipted files are the source of truth; these mirror writes are
# best-effort and must never take down a mark/rebalance that already
# succeeded on disk. A DB hiccup gets printed as a JSON error line (same
# shape as the loop's other error events) and swallowed, not raised.
def _mirror_session_to_store(session_id: str, session: dict[str, Any]) -> None:
    try:
        from paper_store import get_store
        get_store().upsert_session(session_id, session, session.get("cash_accounting_note"))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"event": "db_mirror_error", "op": "session", "error": str(exc)}), flush=True)


def _mirror_trade_to_store(session_id: str, trade: dict[str, Any]) -> None:
    try:
        from paper_store import get_store
        get_store().insert_trade(session_id, trade)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"event": "db_mirror_error", "op": "trade", "error": str(exc)}), flush=True)


def _mirror_mark_to_store(session_id: str, mark: dict[str, Any]) -> None:
    try:
        from paper_store import get_store
        get_store().insert_mark(session_id, mark)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"event": "db_mirror_error", "op": "mark", "error": str(exc)}), flush=True)


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ccxt_symbol(code: str) -> str:
    return code.replace("-", "/").upper()


def _get_exchange():
    import ccxt
    return ccxt.binance({"enableRateLimit": True, "timeout": 15_000})


_OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
_BYBIT_TICKER_URL = "https://api.bybit.com/v5/market/tickers"
_GATE_TICKERS_URL = "https://api.gateio.ws/api/v4/futures/usdt/tickers"

def _fetch_last_prices_binance(symbols: list[str]) -> dict[str, float]:
    all_tickers = request_json("/fapi/v1/ticker/price")
    by_symbol = {t["symbol"]: float(t["price"]) for t in all_tickers if isinstance(t, dict)}
    prices: dict[str, float] = {}
    for code in symbols:
        key = normalize_symbol(code)
        if key not in by_symbol:
            raise RuntimeError(f"No Binance futures price for {code}")
        prices[code] = by_symbol[key]
    return prices


def _fetch_last_prices_okx(symbols: list[str]) -> dict[str, float]:
    """OKX USDT-margined perpetual swaps, e.g. BTC-USDT -> instId BTC-USDT-SWAP.

    Same shape of data as the Binance path (one price per symbol, current
    last trade) but a different exchange's order flow -- expect marks made
    on this source to diverge slightly from what Binance would have shown
    for the same instant. That's the honest cost of falling back, not a bug.
    """
    req = urllib.request.Request(
        _OKX_TICKERS_URL,
        headers={"User-Agent": "MoStar-Futures-Paper/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    by_inst_id = {
        row["instId"]: float(row["last"])
        for row in payload.get("data", [])
        if row.get("last")
    }
    prices: dict[str, float] = {}
    for code in symbols:
        inst_id = _ccxt_symbol(code).replace("/", "-") + "-SWAP"
        if inst_id not in by_inst_id:
            raise RuntimeError(f"No OKX swap price for {code} (instId {inst_id})")
        prices[code] = by_inst_id[inst_id]
    return prices


def _fetch_last_prices_gate(symbols: list[str]) -> dict[str, float]:
    """Gate.io USDT perpetual contracts, e.g. BTC-USDT -> BTC_USDT."""
    req = urllib.request.Request(
        _GATE_TICKERS_URL,
        headers={"User-Agent": "MoStar-Futures-Paper/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    by_contract = {
        row["contract"]: float(row["last"])
        for row in payload
        if isinstance(row, dict) and row.get("contract") and row.get("last")
    }
    prices: dict[str, float] = {}
    for code in symbols:
        contract = _ccxt_symbol(code).replace("/", "_")
        if contract not in by_contract:
            raise RuntimeError(f"No Gate.io futures price for {code} (contract {contract})")
        prices[code] = by_contract[contract]
    return prices


def _fetch_last_prices_bybit(symbols: list[str]) -> dict[str, float]:
    """Bybit linear USDT perpetual tickers."""
    prices: dict[str, float] = {}
    for code in symbols:
        query = urllib.parse.urlencode({"category": "linear", "symbol": normalize_symbol(code)})
        req = urllib.request.Request(
            f"{_BYBIT_TICKER_URL}?{query}",
            headers={"User-Agent": "MoStar-Futures-Paper/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("result", {}).get("list", [])
        if not rows or not rows[0].get("lastPrice"):
            raise RuntimeError(f"No Bybit linear futures price for {code}")
        prices[code] = float(rows[0]["lastPrice"])
    return prices


def _validated_prices(provider: str, fetcher, symbols: list[str]) -> dict[str, float]:
    prices = fetcher(symbols)
    missing = [symbol for symbol in symbols if symbol not in prices]
    invalid = [
        symbol for symbol, price in prices.items()
        if not math.isfinite(float(price)) or float(price) <= 0
    ]
    if missing or invalid:
        raise RuntimeError(
            f"{provider} returned an invalid price snapshot "
            f"(missing={missing}, invalid={invalid})"
        )
    return {symbol: float(prices[symbol]) for symbol in symbols}


@dataclass(frozen=True, slots=True)
class PriceFetchResult:
    """Prices plus which exchange actually answered -- see fetch_last_prices_with_source."""
    prices: dict[str, float]
    source: Literal["okx", "binance", "bybit", "gate"]


def fetch_last_prices_with_source(symbols: list[str]) -> PriceFetchResult:
    """Fetch the current last-trade price for each symbol, with provenance.

    OKX is primary, followed by Binance, Bybit, and Gate. A provider is
    accepted only when it returns a complete, finite, positive snapshot for
    every requested symbol. Each failed attempt is logged before trying the
    next provider; callers persist the provider that actually answered.
    """
    providers = [
        ("okx", _fetch_last_prices_okx),
        ("binance", _fetch_last_prices_binance),
        ("bybit", _fetch_last_prices_bybit),
        ("gate", _fetch_last_prices_gate),
    ]
    failures: list[str] = []
    for index, (provider, fetcher) in enumerate(providers):
        try:
            prices = _validated_prices(provider, fetcher, symbols)
            return PriceFetchResult(prices=prices, source=provider)
        except Exception as exc:  # provider failure must not be mislabeled as success
            failures.append(f"{provider}: {exc}")
            if index + 1 < len(providers):
                print(json.dumps({
                    "event": "price_source_fallback",
                    "from": provider,
                    "to": providers[index + 1][0],
                    "reason": str(exc),
                    "timestamp": _now_iso(),
                }), flush=True)
    raise RuntimeError("No fresh futures price provider available: " + "; ".join(failures))


def fetch_last_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch the current last-trade price for each symbol.

    Thin wrapper over fetch_last_prices_with_source() that drops provenance,
    kept for the many existing callers/tests that only need the price map.
    """
    return fetch_last_prices_with_source(symbols).prices


_fast_exchange_cache: dict[str, Any] = {}


def _get_cached_exchange():
    """Reuse one exchange instance across calls -- ccxt's implicit
    load_markets() on first use costs ~3s; a fresh instance per call would
    pay that on every poll instead of once per process."""
    if "exchange" not in _fast_exchange_cache:
        _fast_exchange_cache["exchange"] = _get_exchange()
    return _fast_exchange_cache["exchange"]


def _get_cached_futures_exchange():
    """Cached Binance USDT-M Futures exchange for funding-rate calls."""
    if "futures" not in _fast_exchange_cache:
        import ccxt
        _fast_exchange_cache["futures"] = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 15_000})
    return _fast_exchange_cache["futures"]


def fetch_last_prices_fast(symbols: list[str]) -> dict[str, float]:
    """Batch-fetch current prices in one exchange call instead of one per symbol.

    For frequent live-ticker polling (every few seconds) where per-symbol
    round trips would add up. Ledger marks keep using fetch_last_prices() --
    this is display-only and never touches a session's receipted files.
    """
    _binance_limiter.acquire()
    exchange = _get_cached_exchange()
    ccxt_symbols = [_ccxt_symbol(code) for code in symbols]
    tickers = exchange.fetch_tickers(ccxt_symbols)
    prices: dict[str, float] = {}
    for code, ccxt_symbol in zip(symbols, ccxt_symbols):
        ticker = tickers.get(ccxt_symbol)
        last = ticker.get("last") if ticker else None
        if last is None:
            raise RuntimeError(f"No last price in Binance ticker for {code}")
        prices[code] = float(last)
    return prices


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line, then re-receipt the whole file.

    A receipted_write() of the full updated file (instead of a raw append)
    means the file's hash always reflects exactly what's on disk -- tamper-
    evident the same way marks.jsonl already was.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = existing + json.dumps(record, ensure_ascii=False) + "\n"
    receipted_write(path, updated)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── Risk management defaults ────────────────────────────────────────────────

DEFAULT_RISK_CONFIG: dict[str, Any] = {
    "take_profit_pct": None,        # e.g. 0.05 = close at +5% from entry
    "stop_loss_pct": None,          # e.g. 0.03 = close at -3% from entry
    "trailing_stop_pct": None,      # e.g. 0.02 = trail 2% from high-water mark
    "max_hold_hours": None,         # e.g. 24 = force-close after 24h
    "leverage": 5.0,                # 5x or 10x for futures margin mode
    "margin_mode": "isolated",      # "cross" or "isolated" (default: isolated)
    "liquidation_buffer_pct": 0.10, # safety margin for liquidation calc
    "fixed_margin_per_trade": 0.0,  # 0 = disabled (equal-weight/percentage sizing);
                                     # opt in with 20-100 to switch a session to
                                     # fixed-margin futures sizing. Matches the CLI's
                                     # own --fixed-margin default -- see start_session.
    "portfolio_leverage": False,    # Explicit opt-in for full-account
                                     # leveraged equal-weight exposure.
}


def _default_risk_config(**overrides: Any) -> dict[str, Any]:
    cfg = dict(DEFAULT_RISK_CONFIG)
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


def _compute_liquidation_price(
    entry_price: float,
    leverage: float,
    direction: int,
    buffer_pct: float = 0.10,
) -> float:
    """Compute liquidation price for a leveraged position.

    For a long: liq_price = entry * (1 - 1/leverage + buffer)
    For a short: liq_price = entry * (1 + 1/leverage - buffer)
    """
    if leverage <= 1.0:
        return 0.0  # no liquidation for spot
    margin_fraction = 1.0 / leverage
    if direction > 0:  # long
        return entry_price * (1.0 - margin_fraction + buffer_pct)
    else:  # short
        return entry_price * (1.0 + margin_fraction - buffer_pct)


def _init_position_metadata(
    symbol: str,
    qty: float,
    entry_price: float,
    entry_time: str,
    direction: int = 1,
    leverage: float = 1.0,
    margin_mode: str = "cross",
    liquidation_buffer_pct: float = 0.10,
    margin: float = 0.0,
) -> dict[str, Any]:
    """Create per-position metadata for risk tracking."""
    return {
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "entry_time": entry_time,
        "direction": direction,  # 1=long, -1=short
        "high_water_mark": entry_price if direction > 0 else entry_price,
        "low_water_mark": entry_price if direction < 0 else entry_price,
        "leverage": leverage,
        "margin": margin,
        "margin_mode": margin_mode,
        "liquidation_price": _compute_liquidation_price(
            entry_price, leverage, direction, liquidation_buffer_pct,
        ),
    }


@dataclass(frozen=True)
class CloseIntent:
    """A detected reason to close a position. Carries no settlement logic --
    _execute_close_intent (backed by accounting.futures_ledger) is the only
    code path that turns this into a cash/margin mutation, so the reason
    string can never select a different financial formula. See the
    forensic audit of paper_sessions/funding_live: trailing_stop and
    max_hold_expired exits used to apply a different (broken) cash formula
    than funding_z_exit/rebalance closes on the same position shape.
    """

    symbol: str
    quantity: float
    reason: str
    mark_price: float


def _check_risk_exits(
    session: dict[str, Any],
    book: dict[str, Any],
    prices: dict[str, float],
    now: str,
    session_dir: Path,
) -> list[CloseIntent]:
    """Check all open positions against TP/SL/trailing/max-hold/liquidation.

    Detection only. Never mutates cash_remaining, reserved_margin,
    positions, fees, realized P&L, trades, or marks -- it only updates each
    position's trailing high/low-water-mark bookkeeping (needed to detect a
    trailing-stop breach on the *next* call) and returns the list of
    positions that should close. The caller executes each CloseIntent via
    _execute_close_intent, the single settlement path shared with every
    other close reason.
    """
    risk = session.get("risk_config", DEFAULT_RISK_CONFIG)
    tp_pct = risk.get("take_profit_pct")
    sl_pct = risk.get("stop_loss_pct")
    trailing_pct = risk.get("trailing_stop_pct")
    max_hold_hours = risk.get("max_hold_hours")

    positions = book["positions"]
    pos_meta = book.get("position_metadata", {})

    intents: list[CloseIntent] = []
    now_dt = _parse_iso(now)

    for sym in list(positions.keys()):
        qty = positions.get(sym, 0.0)
        if abs(qty) < 1e-12:
            continue

        price = prices.get(sym)
        if price is None:
            continue

        meta = pos_meta.get(sym, {})
        entry_price = meta.get("entry_price", 0.0)
        entry_time_str = meta.get("entry_time")
        direction = meta.get("direction", 1 if qty > 0 else -1)
        hwm = meta.get("high_water_mark", entry_price)
        lwm = meta.get("low_water_mark", entry_price)
        liq_price = meta.get("liquidation_price", 0.0)
        leverage = float(meta.get("leverage", risk.get("leverage", 1.0)))

        exit_reason = None

        # 1. Liquidation check (leveraged positions only)
        if leverage > 1.0 and liq_price > 0:
            if direction > 0 and price <= liq_price:
                exit_reason = "liquidation"
            elif direction < 0 and price >= liq_price:
                exit_reason = "liquidation"

        # 2. Take profit
        if exit_reason is None and tp_pct is not None and entry_price > 0:
            pnl_pct = (price - entry_price) / entry_price * direction
            if pnl_pct >= tp_pct:
                exit_reason = "take_profit"

        # 3. Stop loss
        if exit_reason is None and sl_pct is not None and entry_price > 0:
            pnl_pct = (price - entry_price) / entry_price * direction
            if pnl_pct <= -sl_pct:
                exit_reason = "stop_loss"

        # 4. Trailing stop
        if exit_reason is None and trailing_pct is not None and entry_price > 0:
            if direction > 0:
                new_hwm = max(hwm, price)
                trail_level = new_hwm * (1.0 - trailing_pct)
                if price <= trail_level and new_hwm > entry_price:
                    exit_reason = "trailing_stop"
                meta["high_water_mark"] = new_hwm
                pos_meta[sym] = meta
            else:
                new_lwm = min(lwm, price)
                trail_level = new_lwm * (1.0 + trailing_pct)
                if price >= trail_level and new_lwm < entry_price:
                    exit_reason = "trailing_stop"
                meta["low_water_mark"] = new_lwm
                pos_meta[sym] = meta

        # 5. Max hold duration
        if exit_reason is None and max_hold_hours is not None and entry_time_str:
            entry_dt = _parse_iso(entry_time_str)
            hold_duration = now_dt - entry_dt
            if hold_duration >= timedelta(hours=max_hold_hours):
                exit_reason = "max_hold_expired"

        if exit_reason is not None:
            intents.append(CloseIntent(symbol=sym, quantity=abs(qty), reason=exit_reason, mark_price=price))

    book["position_metadata"] = pos_meta
    return intents


def _book_position_to_ledger(symbol: str, qty: float, meta: dict[str, Any]) -> LedgerPosition:
    side = LedgerSide.LONG if qty > 0 else LedgerSide.SHORT
    leverage = meta.get("leverage") or 1.0
    return LedgerPosition(
        position_id=symbol,
        symbol=symbol,
        side=side,
        quantity=ledger_dec(str(abs(qty))),
        entry_price=ledger_dec(str(meta.get("entry_price", 0.0))),
        leverage=ledger_dec(str(max(float(leverage), 1.0))),
        margin_reserved=ledger_dec(str(meta.get("margin", 0.0))),
    )


def _execute_close_intent(
    session: dict[str, Any],
    book: dict[str, Any],
    session_dir: Path,
    intent: CloseIntent,
    now: str,
) -> dict[str, Any]:
    """The single settlement path for every close reason: funding_z_exit,
    rebalance, take_profit, stop_loss, trailing_stop, max_hold_expired,
    liquidation, manual paper close. Raises AccountingInvariantError/
    ValueError (from accounting.futures_ledger) before touching `book` if
    the close would violate wallet or margin conservation -- nothing is
    persisted on failure because the caller only writes `book`/trades.jsonl
    after this function returns successfully.
    """
    positions = book["positions"]
    pos_meta = book.get("position_metadata", {})
    meta = pos_meta.get(intent.symbol, {})
    qty = positions.get(intent.symbol, 0.0)

    fee_rate = session.get("fee_rate", 0.0)
    slippage_rate = session.get("slippage_rate", 0.0)
    direction = meta.get("direction", 1 if qty > 0 else -1)

    account = LedgerAccount(
        available_cash=ledger_dec(str(book.get("cash_remaining", 0.0))),
        reserved_margin=ledger_dec(str(book.get("reserved_margin", 0.0))),
    )
    position = _book_position_to_ledger(intent.symbol, qty, meta)

    slippage_bps = ledger_dec(str(slippage_rate * 10000))
    action = "sell" if direction > 0 else "buy"
    execution_price, _slippage_cost = (
        ledger_apply_slippage(mark_price=str(intent.mark_price), action=action, slippage_bps=slippage_bps)
        if slippage_rate > 0
        else (ledger_dec(str(intent.mark_price)), ledger_dec("0"))
    )

    result = ledger_close_position(
        account=account,
        position=position,
        close_quantity=str(intent.quantity),
        execution_price=execution_price,
        fee_rate=str(fee_rate),
    )

    # Nothing above touched `book` -- only now, after the ledger call
    # succeeded, do we mutate it.
    book["cash_remaining"] = float(result.account.available_cash)
    book["reserved_margin"] = float(result.account.reserved_margin)
    positions[intent.symbol] = 0.0
    if intent.symbol in pos_meta:
        del pos_meta[intent.symbol]
    if abs(positions[intent.symbol]) < 1e-12:
        del positions[intent.symbol]
    book["positions"] = positions
    book["position_metadata"] = pos_meta

    entry_time_str = meta.get("entry_time")
    now_dt = _parse_iso(now)
    trade = {
        "timestamp": now,
        "symbol": intent.symbol,
        "side": "SELL" if direction > 0 else "BUY",
        "qty": float(result.closed_quantity),
        "price": float(execution_price),
        "notional": float(result.exit_notional),
        "fee_paid": float(result.exit_fee),
        "gross_pnl": float(result.gross_pnl),
        "net_pnl": float(result.net_pnl),
        "margin": float(result.released_margin),
        "reason": intent.reason,
        "entry_price": float(position.entry_price),
        "hold_hours": (now_dt - _parse_iso(entry_time_str)).total_seconds() / 3600 if entry_time_str else None,
    }
    return trade


def _execute_open_position(
    session: dict[str, Any],
    book: dict[str, Any],
    symbol: str,
    side: LedgerSide,
    target_notional: float,
    mark_price: float,
    now: str,
    leverage: float,
    margin_mode: str,
    liquidation_buffer_pct: float,
    reason: str,
) -> dict[str, Any]:
    """Shared entry path backed by accounting.futures_ledger.open_position:
    reserves margin (notional / leverage) instead of debiting/crediting full
    notional as cash -- the funding_rate_zscore engine's historical bug was
    never reserving margin at all (see forensic audit of
    paper_sessions/funding_live), which is also why _check_risk_exits's
    margin release was always a no-op for those positions. Raises
    ValueError (insufficient cash) before touching `book` if unaffordable.
    """
    fee_rate = session.get("fee_rate", 0.0)
    slippage_rate = session.get("slippage_rate", 0.0)
    lev = max(float(leverage), 1.0)
    quantity = target_notional / mark_price

    slippage_bps = ledger_dec(str(slippage_rate * 10000))
    action = "buy" if side is LedgerSide.LONG else "sell"
    execution_price, _slippage_cost = (
        ledger_apply_slippage(mark_price=str(mark_price), action=action, slippage_bps=slippage_bps)
        if slippage_rate > 0
        else (ledger_dec(str(mark_price)), ledger_dec("0"))
    )

    account = LedgerAccount(
        available_cash=ledger_dec(str(book.get("cash_remaining", 0.0))),
        reserved_margin=ledger_dec(str(book.get("reserved_margin", 0.0))),
    )

    result = ledger_open_position(
        account=account,
        position_id=symbol,
        symbol=symbol,
        side=side,
        quantity=str(quantity),
        execution_price=execution_price,
        leverage=str(lev),
        fee_rate=str(fee_rate),
    )

    # Nothing above touched `book` -- only now, after the ledger call
    # succeeded, do we mutate it.
    book["cash_remaining"] = float(result.account.available_cash)
    book["reserved_margin"] = float(result.account.reserved_margin)
    positions = book["positions"]
    positions[symbol] = float(result.position.quantity) if side is LedgerSide.LONG else -float(result.position.quantity)
    book["positions"] = positions
    pos_meta = book.get("position_metadata", {})
    pos_meta[symbol] = _init_position_metadata(
        symbol, float(result.position.quantity), float(result.position.entry_price), now,
        direction=1 if side is LedgerSide.LONG else -1, leverage=lev, margin_mode=margin_mode,
        liquidation_buffer_pct=liquidation_buffer_pct,
        margin=float(result.position.margin_reserved),
    )
    book["position_metadata"] = pos_meta

    return {
        "timestamp": now, "symbol": symbol,
        "side": "BUY" if side is LedgerSide.LONG else "SELL",
        "qty": float(result.position.quantity), "price": float(execution_price),
        "notional": float(result.entry_notional), "fee_paid": float(result.entry_fee),
        "margin": float(result.position.margin_reserved),
        "reason": reason,
    }


def _execute_risk_exit_intents(
    session: dict[str, Any],
    book: dict[str, Any],
    session_dir: Path,
    intents: list[CloseIntent],
    now: str,
) -> list[dict[str, Any]]:
    """Runs each detected CloseIntent through the shared settlement path and
    appends the resulting trade to trades.jsonl. Caller still persists
    book.json afterward (unchanged from the pre-refactor contract)."""
    trades: list[dict[str, Any]] = []
    for intent in intents:
        trade = _execute_close_intent(session, book, session_dir, intent, now)
        trades.append(trade)
        _append_jsonl(session_dir / "trades.jsonl", trade)
        _mirror_trade_to_store(session_dir.name, trade)
    return trades


def start_session(
    session_dir: Path,
    symbols: list[str],
    initial_cash: float,
    rebalance_interval_hours: float,
    fee_rate: float = 0.0,
    min_rebalance_notional: float = 0.0,
    entry_prices: Optional[dict[str, float]] = None,
    entry_time: Optional[str] = None,
    *,
    risk_config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Enter an equal-weight position in every symbol at the current live price.

    Refuses to start over an existing session directory -- a fresh start
    must not silently overwrite a session already in progress.

    ``fee_rate`` (e.g. 0.001 for Binance's ~0.1% taker fee) is charged on
    every trade's notional, including the entry trades, and drags down
    ``cash_remaining`` -- without it, a shorter rebalance interval always
    looks better by construction (more premium captured, zero cost charged
    against it), which tests nothing about the strategy.

    Entry sizing reserves the fee budget up front: ``investable_cash =
    initial_cash / (1 + fee_rate)`` is split across symbols, so after the
    fee on each entry trade is charged, cash_remaining lands at exactly 0
    instead of going negative by the total entry fee. (An earlier version
    split the full initial_cash across symbols and then charged fees as a
    separate debit, which is why sessions started before this fix show
    negative cash_remaining from their very first mark -- see
    cash_accounting_note on those sessions.)

    ``min_rebalance_notional`` (default 0.0)
    suppresses any per-symbol rebalance trade whose dollar delta is below
    it -- see notrade_band_experiment.py's offline finding that at
    min_notional=$10, rebalance_value_added (return vs. buy-and-hold of the
    same entry basket) turns consistently positive across all 3 historical
    sessions, at the cost of forgoing ~98% of rebalance trades. This is a
    session-level config choice precisely so a control (0.0) and a
    candidate (10.0) session can run side by side from the same start
    conditions for a shadow A/B, per session_reconciliation.py's schema
    versioning -- neither value is hardcoded as "the" threshold here.

    ``entry_prices``, when given, is used verbatim instead of fetching a
    fresh quote, and ``entry_time`` likewise instead of stamping a fresh
    timestamp. A paired control/candidate A/B must enter on the *same*
    quote at the *same* stamped moment -- two independent
    ``fetch_last_prices`` calls (and independently stamped entry times) a
    few seconds apart let quote-timing noise contaminate the comparison
    (see run_paired_loop / start_paired_sessions, which fetch once and
    stamp once, then pass the same values into every session in the pair).
    """
    if session_dir.exists():
        raise FileExistsError(f"session_dir already exists: {session_dir}")
    session_dir.mkdir(parents=True)

    entry_prices = dict(entry_prices) if entry_prices is not None else fetch_last_prices(symbols)
    entry_time = entry_time if entry_time is not None else _now_iso()

    effective_risk = _default_risk_config(**(risk_config or {}))
    leverage = float(effective_risk.get("leverage", 5.0))
    margin_mode = effective_risk.get("margin_mode", "isolated")
    liq_buffer = float(effective_risk.get("liquidation_buffer_pct", 0.10))
    fixed_margin = float(effective_risk.get("fixed_margin_per_trade", 0.0))
    portfolio_leverage = bool(effective_risk.get("portfolio_leverage", False))

    # Validate futures margin settings. 0 means fixed-margin sizing is
    # disabled (equal-weight/percentage sizing below) -- only a nonzero,
    # explicitly-opted-in value is range-checked.
    if fixed_margin != 0.0 and (fixed_margin < 20.0 or fixed_margin > 100.0):
        raise ValueError(f"fixed_margin_per_trade must be 0 (disabled) or between 20 and 100 USD, got {fixed_margin}")
    if leverage not in (1.0, 5.0, 10.0):
        raise ValueError(f"leverage must be 1, 5 or 10, got {leverage}")
    if margin_mode not in ("isolated", "cross"):
        raise ValueError(f"margin_mode must be isolated or cross, got {margin_mode}")
    if portfolio_leverage and fixed_margin > 0:
        raise ValueError("portfolio_leverage and fixed_margin_per_trade are mutually exclusive")

    if portfolio_leverage:
        total_notional = initial_cash / ((1.0 / leverage) + fee_rate)
        per_symbol_notional = total_notional / len(symbols)
        per_symbol_margin = per_symbol_notional / leverage
        positions = {code: per_symbol_notional / price for code, price in entry_prices.items()}
        entry_fee = per_symbol_notional * fee_rate
    elif fixed_margin > 0:
        # Fixed-margin futures mode: reserve fixed_margin per symbol, position notional = margin * leverage
        per_symbol_notional = fixed_margin * leverage
        per_symbol_margin = fixed_margin
        positions = {code: per_symbol_notional / price for code, price in entry_prices.items()}
        entry_fee = per_symbol_notional * fee_rate
        required_cash = per_symbol_margin * len(symbols) + entry_fee * len(symbols)
        if required_cash > initial_cash:
            raise ValueError(f"insufficient initial cash: need ${required_cash:.2f} for {len(symbols)} positions at ${fixed_margin} margin, have ${initial_cash:.2f}")
    else:
        # Equal-weight mode: split investable cash equally
        investable_cash = initial_cash / (1 + fee_rate) if fee_rate else initial_cash
        per_symbol_margin = investable_cash / len(symbols)
        per_symbol_notional = per_symbol_margin
        positions = {code: per_symbol_margin / price for code, price in entry_prices.items()}
        entry_fee = per_symbol_margin * fee_rate

    session = {
        "strategy_type": STRATEGY_TYPE,
        "symbols": symbols,
        "initial_cash": initial_cash,
        "entry_time": entry_time,
        "entry_prices": entry_prices,
        "rebalance_interval_hours": rebalance_interval_hours,
        "fee_rate": fee_rate,
        "source": "binance",
        "price_kind": "live_ticker_last",
        "fees_modeled": fee_rate > 0,
        "slippage_modeled": False,
        "cash_accounting_note": None,
        "accounting_schema_version": ACCOUNTING_SCHEMA_VERSION,
        "accounting_status": "OK",
        "min_rebalance_notional": min_rebalance_notional,
        "risk_config": effective_risk,
    }
    receipted_write(session_dir / "session.json", json.dumps(session, indent=2))
    _mirror_session_to_store(session_dir.name, session)

    cash_remaining = initial_cash - (per_symbol_margin * len(symbols)) - (entry_fee * len(symbols))
    if abs(cash_remaining) < 1e-9:
        cash_remaining = 0.0
    reserved_margin = per_symbol_margin * len(symbols)

    # Build per-position metadata for risk tracking. Equal-weight/spot
    # positions (fixed_margin disabled) are unleveraged -- cost basis equals
    # notional, nothing is borrowed -- so they get leverage=1.0 (no
    # liquidation, per _compute_liquidation_price's spot branch) regardless
    # of risk_config's leverage, which only applies once fixed-margin futures
    # sizing is actually selected. Otherwise an ordinary price move on an
    # unleveraged position could trip a synthetic 5x liquidation exit.
    position_leverage = leverage if (portfolio_leverage or fixed_margin > 0) else 1.0
    pos_meta: dict[str, Any] = {}
    for code in symbols:
        pos_meta[code] = _init_position_metadata(
            code, positions[code], entry_prices[code], entry_time,
            direction=1, leverage=position_leverage, margin_mode=margin_mode,
            liquidation_buffer_pct=liq_buffer,
            margin=per_symbol_margin,
        )

    book = {
        "positions": positions,
        "cash_remaining": cash_remaining,
        "reserved_margin": reserved_margin,
        "last_rebalance_time": entry_time,
        "position_metadata": pos_meta,
    }
    receipted_write(session_dir / "book.json", json.dumps(book, indent=2))

    for code in symbols:
        entry_trade = {
            "timestamp": entry_time,
            "symbol": code,
            "side": "BUY",
            "qty": positions[code],
            "price": entry_prices[code],
            "notional": per_symbol_notional,
            "fee_paid": entry_fee,
            "reason": "entry",
        }
        _append_jsonl(session_dir / "trades.jsonl", entry_trade)
        _mirror_trade_to_store(session_dir.name, entry_trade)

    entry_mark = _build_mark(session, book, entry_prices, now=entry_time)
    _append_jsonl(session_dir / "marks.jsonl", entry_mark)
    _mirror_mark_to_store(session_dir.name, entry_mark)
    return session


def _load_session(session_dir: Path) -> dict[str, Any]:
    session_path = session_dir / "session.json"
    intact = verify_receipted_file(session_path)
    if not intact:
        logger.error("Integrity check failed for %s", session_path)
    session = json.loads(session_path.read_text(encoding="utf-8"))
    if not intact:
        session["tampered"] = True
    return session


def _load_book(session_dir: Path) -> dict[str, Any]:
    book_path = session_dir / "book.json"
    if not verify_receipted_file(book_path):
        logger.error("Integrity check failed for %s", book_path)
    return json.loads(book_path.read_text(encoding="utf-8"))


def _build_mark(
    session: dict[str, Any],
    book: dict[str, Any],
    prices: dict[str, float],
    *,
    now: Optional[str] = None,
) -> dict[str, Any]:
    positions = book["positions"]
    pos_meta = book.get("position_metadata", {})
    position_values = {code: positions.get(code, 0.0) * prices[code] for code in session["symbols"]}
    initial_cash = session["initial_cash"]
    risk = session.get("risk_config", {})

    # Per-position unrealized P&L and margin
    position_pnl: dict[str, float] = {}
    reserved_margin = 0.0
    open_notional = 0.0
    for code in session["symbols"]:
        meta = pos_meta.get(code, {})
        entry_price = meta.get("entry_price", 0.0)
        direction = meta.get("direction", 1 if positions.get(code, 0.0) > 0 else -1)
        qty = positions.get(code, 0.0)
        price = prices.get(code, 0.0)
        if entry_price > 0 and abs(qty) > 1e-12:
            position_pnl[code] = (price - entry_price) * qty * direction
            open_notional += abs(qty * price)
            reserved_margin += float(meta.get("margin", abs(qty * entry_price / meta.get("leverage", 1.0))))
        else:
            position_pnl[code] = 0.0

    cash = float(book.get("cash_remaining", 0.0))
    total_unrealized = sum(position_pnl.values())
    wallet_balance = cash + reserved_margin
    available_balance = cash
    # Equity = cash + market value of all positions (fundamental definition).
    # When position_metadata is populated, this equals cash + reserved + unrealized
    # since reserved = cost_basis and unrealized = market_value - cost_basis.
    # When metadata is empty (legacy sessions), this still gives the correct equity.
    if risk.get("portfolio_leverage", False) or float(risk.get("fixed_margin_per_trade", 0.0)) > 0:
        equity = cash + reserved_margin + total_unrealized
    else:
        equity = cash + sum(position_values.values())

    return {
        "timestamp": now if now is not None else _now_iso(),
        "prices": prices,
        "position_values": position_values,
        "position_pnl": position_pnl,
        "cash_remaining": cash,
        "reserved_margin": reserved_margin,
        "open_notional": open_notional,
        "wallet_balance": wallet_balance,
        "available_balance": available_balance,
        "unrealized_pnl": total_unrealized,
        "equity": equity,
        "pnl": equity - initial_cash,
        "pnl_pct": (equity - initial_cash) / initial_cash if initial_cash else 0.0,
        "leverage": risk.get("leverage", 5.0),
        "margin_mode": risk.get("margin_mode", "isolated"),
    }


def mark_once(
    session_dir: Path,
    *,
    prices: Optional[dict[str, float]] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Compute one mark-to-market snapshot, append it, return it.

    ``prices`` and ``now``, when given, are used verbatim instead of
    fetching a fresh quote / stamping a fresh timestamp -- lets a paired
    runner mark multiple sessions off one shared fetch and one shared clock
    read (see run_paired_loop).
    """
    session = _load_session(session_dir)
    book = _load_book(session_dir)
    prices = dict(prices) if prices is not None else fetch_last_prices(session["symbols"])
    mark = _build_mark(session, book, prices, now=now)
    _append_jsonl(session_dir / "marks.jsonl", mark)
    _mirror_mark_to_store(session_dir.name, mark)
    return mark


@exclusive_session_mutation
def rebalance_if_due(
    session_dir: Path,
    *,
    force: bool = False,
    prices: Optional[dict[str, float]] = None,
    now: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Rebalance back to equal weight if the interval has elapsed (or ``force``).

    Returns the list of executed trades (may be empty if already balanced
    within ``MIN_TRADE_NOTIONAL``), or None if not due yet.

    ``prices`` and ``now``, when given, are used verbatim instead of
    fetching a fresh quote / stamping a fresh timestamp. This is the
    mechanism a paired control/candidate A/B run uses to give both ledgers
    the exact same quote at the exact same stamped moment for a rebalance
    decision, instead of two independent ``fetch_last_prices`` calls (and
    independently stamped timestamps) a few seconds apart (see
    run_paired_loop).
    """
    session = _load_session(session_dir)
    if session.get("accounting_status") == "ACCOUNTING_ERROR":
        raise RuntimeError(
            f"session {session_dir.name} is paused: accounting invariant violated "
            "(equity != initial_cash + realized_pnl + unrealized_pnl). Manual "
            "reconciliation required -- see session_reconciliation.py -- before "
            "further rebalancing."
        )
    book = _load_book(session_dir)
    last_rebalance = _parse_iso(book["last_rebalance_time"])
    interval = timedelta(hours=session["rebalance_interval_hours"])
    wall_clock_now = datetime.now(timezone.utc)
    timestamp = now if now is not None else _now_iso()

    # Risk checks run on every call, even when rebalance is not due.
    prices = dict(prices) if prices is not None else fetch_last_prices(session["symbols"])
    risk_exit_intents = _check_risk_exits(session, book, prices, timestamp, session_dir)
    risk_exits = _execute_risk_exit_intents(session, book, session_dir, risk_exit_intents, timestamp)
    if risk_exits:
        # Persist book after risk exits, then continue to rebalance if due
        receipted_write(session_dir / "book.json", json.dumps(book, indent=2))

    if not force and wall_clock_now - last_rebalance < interval:
        # Not due for rebalance, but risk exits may have fired
        if risk_exits:
            mark = _build_mark(session, book, prices, now=timestamp)
            _append_jsonl(session_dir / "marks.jsonl", mark)
            _mirror_mark_to_store(session_dir.name, mark)
            return {"trades": risk_exits, "mark": mark}
        return None

    positions = dict(book["positions"])
    pos_meta = dict(book.get("position_metadata", {}))
    position_values = {code: positions.get(code, 0.0) * prices[code] for code in session["symbols"]}
    fee_rate = session.get("fee_rate", 0.0)
    min_rebalance_notional = session.get("min_rebalance_notional", 0.0)

    risk = session.get("risk_config", DEFAULT_RISK_CONFIG)
    fixed_margin = risk.get("fixed_margin_per_trade", 0.0)
    leverage = float(risk.get("leverage", 1.0))
    portfolio_leverage = bool(risk.get("portfolio_leverage", False))
    leveraged_mode = portfolio_leverage or fixed_margin > 0
    if leveraged_mode:
        unrealized = sum(
            (prices[code] - float(pos_meta.get(code, {}).get("entry_price", prices[code])))
            * float(positions.get(code, 0.0))
            for code in session["symbols"]
        )
        reserved = sum(float(meta.get("margin", 0.0)) for meta in pos_meta.values())
        equity = float(book["cash_remaining"]) + reserved + unrealized
    else:
        equity = book["cash_remaining"] + sum(position_values.values())

    if portfolio_leverage:
        target_value = equity * leverage / len(session["symbols"])
    elif fixed_margin > 0:
        # Fixed-margin mode: each symbol targets fixed_margin * leverage notional.
        # Only enter if cash can cover the margin + fee for new positions.
        target_notional_per_symbol = fixed_margin * leverage
        target_value = target_notional_per_symbol
    else:
        # Equal-weight mode: target is equity / n_symbols
        target_value = equity / len(session["symbols"])

    executed: list[dict[str, Any]] = []

    # Journal checkpoint before any state change
    _append_journal(session_dir, {
        "type": "checkpoint",
        "state": {"cash": float(book["cash_remaining"]), "positions": dict(positions)},
    })

    # Build the rebalance plan respecting the no-trade band and dust floor.
    planned: list[tuple[str, float]] = []
    for code in session["symbols"]:
        delta_value = target_value - position_values[code]
        if abs(delta_value) < MIN_TRADE_NOTIONAL:
            continue  # dust floor -- always applies, independent of the no-trade band
        if abs(delta_value) < min_rebalance_notional:
            continue
        planned.append((code, delta_value))

    sells = [(code, dv) for code, dv in planned if dv < 0]
    buys = [(code, dv) for code, dv in planned if dv > 0]

    cash_remaining = float(book["cash_remaining"])

    # Execute all sells first so their proceeds are available for buys.
    for code, delta_value in sells:
        notional = abs(delta_value)
        fee_paid = notional * fee_rate
        delta_qty = delta_value / prices[code]  # negative
        trade = {
            "timestamp": timestamp,
            "symbol": code,
            "side": "SELL",
            "qty": abs(delta_qty),
            "price": prices[code],
            "notional": notional,
            "fee_paid": fee_paid,
            "reason": "rebalance",
        }
        old_qty = float(positions.get(code, 0.0))
        if leveraged_mode:
            meta = pos_meta[code]
            close_qty = min(old_qty, abs(delta_qty))
            old_margin = float(meta.get("margin", 0.0))
            margin_released = old_margin * close_qty / old_qty if old_qty else 0.0
            realized_pnl = (prices[code] - float(meta["entry_price"])) * close_qty
            cash_remaining += margin_released + realized_pnl - fee_paid
            meta["margin"] = max(0.0, old_margin - margin_released)
            meta["quantity"] = max(0.0, old_qty - close_qty)
        else:
            cash_remaining += notional - fee_paid
        positions[code] = old_qty + delta_qty
        executed.append(trade)
        _append_jsonl(session_dir / "trades.jsonl", trade)
        _mirror_trade_to_store(session_dir.name, trade)

    # After sells settle, this is the only cash we can spend on buys.
    buy_budget = max(0.0, cash_remaining)
    remaining_budget = buy_budget

    if fixed_margin > 0 and not portfolio_leverage:
        # Fixed-margin mode: only buy if cash covers the full margin + fee for that position.
        # Don't scale down — either fund the full position or skip it.
        affordable_buys: list[tuple[str, float]] = []
        for code, dv in buys:
            margin_needed = fixed_margin + (dv * fee_rate)
            if remaining_budget >= margin_needed:
                affordable_buys.append((code, dv))
                remaining_budget -= dv * (1 + fee_rate)
        buys = affordable_buys
        remaining_budget = buy_budget

    requested_buy_cost = sum(
        dv * (((1.0 / leverage) + fee_rate) if leveraged_mode else (1 + fee_rate))
        for _, dv in buys
    )
    buy_scale = (
        min(1.0, buy_budget / requested_buy_cost) if requested_buy_cost > 0 else 0.0
    )

    for code, delta_value in buys:
        desired_notional = delta_value * buy_scale
        unit_cost = ((1.0 / leverage) + fee_rate) if leveraged_mode else (1 + fee_rate)
        max_notional = max(0.0, remaining_budget / unit_cost)
        notional = min(desired_notional, max_notional)
        if notional < MIN_TRADE_NOTIONAL:
            continue
        fee_paid = notional * fee_rate
        margin_added = notional / leverage if leveraged_mode else notional
        total_cost = margin_added + fee_paid
        delta_qty = notional / prices[code]  # positive
        trade = {
            "timestamp": timestamp,
            "symbol": code,
            "side": "BUY",
            "qty": delta_qty,
            "price": prices[code],
            "notional": notional,
            "fee_paid": fee_paid,
            "reason": "rebalance",
        }
        old_qty = float(positions.get(code, 0.0))
        if leveraged_mode:
            meta = pos_meta.get(code)
            if meta is None or old_qty <= 1e-12:
                meta = _init_position_metadata(
                    code, delta_qty, prices[code], timestamp,
                    direction=1, leverage=leverage,
                    margin_mode=risk.get("margin_mode", "isolated"),
                    liquidation_buffer_pct=float(risk.get("liquidation_buffer_pct", 0.005)),
                    margin=margin_added,
                )
            else:
                old_entry = float(meta.get("entry_price", prices[code]))
                meta["entry_price"] = (
                    old_qty * old_entry + delta_qty * prices[code]
                ) / (old_qty + delta_qty)
                meta["margin"] = float(meta.get("margin", 0.0)) + margin_added
                meta["quantity"] = old_qty + delta_qty
                meta["liquidation_price"] = _compute_liquidation_price(
                    float(meta["entry_price"]), leverage, 1,
                    float(risk.get("liquidation_buffer_pct", 0.005)),
                )
            pos_meta[code] = meta
        positions[code] = old_qty + delta_qty
        cash_remaining -= total_cost
        remaining_budget -= total_cost
        executed.append(trade)
        _append_jsonl(session_dir / "trades.jsonl", trade)
        _mirror_trade_to_store(session_dir.name, trade)

    if abs(cash_remaining) < 1e-9:
        cash_remaining = 0.0

    # Preserve position_metadata from risk exits and leveraged rebalance updates.
    reserved_margin = sum(float(m.get("margin", 0.0)) for m in pos_meta.values())
    book = {"positions": positions, "cash_remaining": cash_remaining, "reserved_margin": reserved_margin, "last_rebalance_time": timestamp, "position_metadata": pos_meta}
    receipted_write(session_dir / "book.json", json.dumps(book, indent=2))

    mark = _build_mark(session, book, prices, now=timestamp)
    _append_jsonl(session_dir / "marks.jsonl", mark)
    _mirror_mark_to_store(session_dir.name, mark)

    _check_and_flag_accounting_invariant(session_dir, session, book, mark)
    _append_journal(session_dir, {"type": "commit"})
    all_trades = risk_exits + executed
    return {"trades": all_trades, "mark": mark}


def _check_and_flag_accounting_invariant(
    session_dir: Path,
    session: dict[str, Any],
    book: dict[str, Any],
    mark: dict[str, Any],
) -> None:
    """Verify equity == initial_cash + realized_pnl + unrealized_pnl after a rebalance.

    This trade/mark has already been receipted -- the ledger is immutable,
    so a violation can't be undone here. Instead it flips
    ``accounting_status`` to ``ACCOUNTING_ERROR`` in session.json, which the
    guard at the top of this function refuses to rebalance past on the next
    call (paper trading should stop opening/rebalancing positions once the
    ledger stops reconciling, rather than keep compounding a broken cash
    trail the way the pre-versioning demo sessions did).
    """
    trades = _read_jsonl(session_dir / "trades.jsonl")
    trade_stats = compute_trade_stats(trades)
    position = _compute_unrealized_position_pnl(trade_stats["by_symbol"], mark["prices"])
    unrealized_pnl = position["unrealized_pnl"]
    equity = float(mark["equity"])
    initial_cash = float(session["initial_cash"])
    realized_pnl = trade_stats["overall"]["realized_pnl"]

    # position_ledger_differences catches book.json positions drifting from
    # what trades.jsonl implies (the kind of legacy state divergence that
    # produced the $2,000 residual this guard was added for) independent of
    # whether the equity identity below happens to still balance.
    differences = position_ledger_differences(book.get("positions", {}), trade_stats["by_symbol"])
    decision = assess_accounting(
        configured_symbols=session["symbols"],
        initial_cash=initial_cash,
        equity=equity,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        stale_mark_symbols=position.get("stale_mark_symbols", []),
        position_differences=differences,
        abs_tolerance=RECONCILIATION_ABS_TOLERANCE,
        rel_tolerance=RECONCILIATION_REL_TOLERANCE,
    )

    if decision.state == "DEFERRED":
        # Incomplete valuation evidence (e.g. a stale mark for an open
        # symbol) is not proof of a broken ledger -- freezing the session on
        # it would stop healthy sessions over a transient price gap. Log and
        # let the next mark retry.
        print(
            json.dumps({
                "event": "accounting_check_deferred",
                "session_id": session_dir.name,
                "reason": decision.reason,
                "stale_mark_symbols": list(decision.stale_mark_symbols),
                "timestamp": mark["timestamp"],
            }),
            flush=True,
        )
        return

    if decision.state == "ERROR":
        session = dict(session)
        session["accounting_status"] = "ACCOUNTING_ERROR"
        session["accounting_error"] = decision.residual
        session["accounting_error_kind"] = decision.reason
        session["accounting_position_differences"] = decision.position_differences
        session["accounting_stale_mark_symbols"] = list(decision.stale_mark_symbols)
        session["accounting_error_detected_at"] = mark["timestamp"]
        receipted_write(session_dir / "session.json", json.dumps(session, indent=2))
        _mirror_session_to_store(session_dir.name, session)
        print(
            json.dumps({
                "event": "accounting_error",
                "session_id": session_dir.name,
                "reconciliation_error": decision.residual,
                "reason": decision.reason,
                "timestamp": mark["timestamp"],
            }),
            flush=True,
        )


_POSITION_EFFECT_TOLERANCE = 1e-9


def _position_effect_label(position_before: float, closing_qty: float, opening_qty: float, position_after: float) -> str:
    """Classify a trade's inventory effect from signed positions.

    Pure function of before/after state so it can't drift from the qty math
    that produced it -- see compute_trade_stats.
    """
    was_long = position_before > _POSITION_EFFECT_TOLERANCE
    was_short = position_before < -_POSITION_EFFECT_TOLERANCE
    if closing_qty > 0 and opening_qty > 0:
        return "FLIP_LONG_TO_SHORT" if was_long else "FLIP_SHORT_TO_LONG"
    if closing_qty > 0:
        closed_fully = abs(position_after) <= _POSITION_EFFECT_TOLERANCE
        if was_long:
            return "CLOSE_LONG" if closed_fully else "REDUCE_LONG"
        return "CLOSE_SHORT" if closed_fully else "REDUCE_SHORT"
    # opening_qty > 0, closing_qty == 0
    if not was_long and not was_short:
        return "OPEN_LONG" if position_after > 0 else "OPEN_SHORT"
    return "INCREASE_LONG" if was_long else "INCREASE_SHORT"


def compute_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Realized win/loss accounting per symbol, signed-position-aware
    weighted-average cost basis.

    Every symbol's running position is signed: positive is long, negative
    is short. A trade's quantity is split into a closing portion (against
    the *existing opposite-side* position, realizing P&L) and an opening
    portion (extending or starting a position in the trade's own
    direction, realizing nothing). A BUY does not inherently mean "open"
    and a SELL does not inherently mean "close" -- a SELL against a flat or
    already-short book opens/extends a short; a BUY against a short book
    covers it. Treating every SELL as a long-side close (the previous
    version of this function) silently priced a short-open against a
    fabricated $0 cost basis, manufacturing a fake realized gain equal to
    the entire notional -- see agent/tests/test_trade_stats_signed_positions.py
    for the funding_live BTC-USDT trade that surfaced this.

    A trade that flips a position (e.g. long 1 BTC -> sell 3 -> short 2 BTC)
    is split: the closing 1 BTC realizes P&L against the old long entry
    price, the opening 2 BTC starts a fresh short at the trade's execution
    price. The trade's fee is split proportionally between the two portions
    (closing_fee = fee * closing_qty / trade_qty); the opening portion's
    fee becomes part of the new position's entry_fee_basis, not charged
    against the realized P&L of the closing portion.

    Returns per-symbol stats plus an overall rollup, and annotates each
    input trade dict (in place, on a copy) with position_before/after,
    position_effect, closed_qty/opened_qty, entry_price/exit_price, and the
    existing gross_pnl/realized_pnl/net_pnl/entry_fee_allocated/total_fees
    keys -- all None when the trade purely opens/extends a position (no
    closing portion), matching the previous BUY-only-opens convention but
    now correctly extended to short-opening SELLs too.
    """
    per_symbol: dict[str, dict[str, Any]] = {}
    annotated: list[dict[str, Any]] = []

    for trade in trades:
        code = trade["symbol"]
        state = per_symbol.setdefault(code, {
            "qty": 0.0, "avg_cost": 0.0, "entry_fee_basis": 0.0,
            "entry_time": None,
            "realized_pnl": 0.0, "win_count": 0, "loss_count": 0, "breakeven_count": 0,
            "gross_win": 0.0, "gross_loss": 0.0, "fees_paid": 0.0,
            "net_win": 0.0, "net_loss": 0.0,
        })
        trade_qty = trade["qty"]
        fee = trade.get("fee_paid", 0.0) or 0.0
        state["fees_paid"] += fee
        annotated_trade = dict(trade)

        position_before = state["qty"]
        signed_trade_qty = trade_qty if trade["side"] == "BUY" else -trade_qty
        opposes = position_before != 0 and (
            (position_before > 0 and signed_trade_qty < 0) or (position_before < 0 and signed_trade_qty > 0)
        )
        closing_qty = min(abs(position_before), trade_qty) if opposes else 0.0
        opening_qty = trade_qty - closing_qty
        closing_fee = fee * (closing_qty / trade_qty) if trade_qty > 0 else 0.0
        opening_fee = fee - closing_fee

        annotated_trade["position_before"] = position_before
        annotated_trade["closed_qty"] = closing_qty
        annotated_trade["opened_qty"] = opening_qty
        annotated_trade["closing_fee"] = closing_fee if closing_qty > 0 else None
        annotated_trade["opening_fee"] = opening_fee if opening_qty > 0 else None
        annotated_trade["avg_entry_before"] = state["avg_cost"] if position_before != 0 else None

        if closing_qty > 0:
            entry_price = state["avg_cost"]
            if position_before > 0:  # closing a long -- this SELL portion realizes vs. the long's cost
                gross_pnl = closing_qty * (trade["price"] - entry_price)
            else:  # covering a short -- this BUY portion realizes vs. the short's cost
                gross_pnl = closing_qty * (entry_price - trade["price"])
            entry_fee_allocated = (
                state["entry_fee_basis"] * (closing_qty / abs(position_before))
                if position_before != 0 and state["entry_fee_basis"] > 0
                else 0.0
            )
            net_pnl = gross_pnl - entry_fee_allocated - closing_fee
            state["entry_fee_basis"] = max(0.0, state["entry_fee_basis"] - entry_fee_allocated)
            state["realized_pnl"] += net_pnl
            if net_pnl > 0:
                state["win_count"] += 1
                state["gross_win"] += gross_pnl
                state["net_win"] += net_pnl
            elif net_pnl < 0:
                state["loss_count"] += 1
                state["gross_loss"] += abs(gross_pnl)
                state["net_loss"] += abs(net_pnl)
            else:
                state["breakeven_count"] += 1
            annotated_trade["gross_pnl"] = gross_pnl
            annotated_trade["entry_fee_allocated"] = entry_fee_allocated
            annotated_trade["total_fees"] = entry_fee_allocated + closing_fee
            annotated_trade["net_pnl"] = net_pnl
            annotated_trade["realized_pnl"] = net_pnl
            annotated_trade["entry_time"] = state.get("entry_time")
            annotated_trade["entry_price"] = entry_price
            annotated_trade["exit_price"] = trade["price"]
        else:
            annotated_trade["gross_pnl"] = None
            annotated_trade["entry_fee_allocated"] = None
            annotated_trade["total_fees"] = fee
            annotated_trade["net_pnl"] = None
            annotated_trade["realized_pnl"] = None
            annotated_trade["exit_price"] = None

        # Remaining magnitude of the pre-existing position after the closing
        # portion (same sign as position_before; zero once fully closed).
        if position_before > 0:
            remaining_before = position_before - closing_qty
        elif position_before < 0:
            remaining_before = position_before + closing_qty
        else:
            remaining_before = 0.0
        if abs(remaining_before) <= _POSITION_EFFECT_TOLERANCE:
            remaining_before = 0.0

        if opening_qty > 0:
            open_direction = 1.0 if signed_trade_qty > 0 else -1.0
            if remaining_before == 0.0:
                # Fresh open, or the closing portion above fully closed the
                # old position and this is a flip into the new direction.
                new_avg_cost = trade["price"]
                new_entry_fee_basis = opening_fee
                new_entry_time = trade["timestamp"]
            else:
                # Same-direction addition -- closing_qty was 0 here (a trade
                # that both closes and opens always fully closes first, so
                # remaining_before is 0 whenever opening_qty > 0 alongside a
                # nonzero closing_qty).
                old_abs = abs(remaining_before)
                new_abs = old_abs + opening_qty
                new_avg_cost = (old_abs * state["avg_cost"] + opening_qty * trade["price"]) / new_abs
                new_entry_fee_basis = state["entry_fee_basis"] + opening_fee
                new_entry_time = state.get("entry_time")
            state["qty"] = open_direction * (abs(remaining_before) + opening_qty)
            state["avg_cost"] = new_avg_cost
            state["entry_fee_basis"] = new_entry_fee_basis
            state["entry_time"] = new_entry_time
            if closing_qty == 0:
                annotated_trade["entry_price"] = new_avg_cost
        else:
            state["qty"] = remaining_before
            if remaining_before == 0.0:
                state["avg_cost"] = 0.0
                state["entry_fee_basis"] = 0.0
                state["entry_time"] = None

        annotated_trade["position_after"] = state["qty"]
        annotated_trade["avg_entry_after"] = state["avg_cost"] if state["qty"] != 0 else None
        annotated_trade["position_effect"] = _position_effect_label(
            position_before, closing_qty, opening_qty, state["qty"],
        )

        annotated.append(annotated_trade)

    symbol_stats = {}
    total_realized = 0.0
    total_wins = 0
    total_losses = 0
    total_breakevens = 0
    total_gross_win = 0.0
    total_gross_loss = 0.0
    total_net_win = 0.0
    total_net_loss = 0.0
    total_fees = 0.0
    for code, state in per_symbol.items():
        trade_count = state["win_count"] + state["loss_count"]
        symbol_stats[code] = {
            "realized_pnl": state["realized_pnl"],
            "win_count": state["win_count"],
            "loss_count": state["loss_count"],
            "breakeven_count": state["breakeven_count"],
            "win_rate": state["win_count"] / trade_count if trade_count else None,
            "avg_win": state["net_win"] / state["win_count"] if state["win_count"] else None,
            "avg_loss": state["net_loss"] / state["loss_count"] if state["loss_count"] else None,
            "profit_factor": state["net_win"] / state["net_loss"] if state["net_loss"] else None,
            "gross_profit_factor": state["gross_win"] / state["gross_loss"] if state["gross_loss"] else None,
            "fees_paid": state["fees_paid"],
            "open_qty": state["qty"],
            "avg_cost": state["avg_cost"],
            "entry_fee_basis": state["entry_fee_basis"],
        }
        total_realized += state["realized_pnl"]
        total_wins += state["win_count"]
        total_losses += state["loss_count"]
        total_breakevens += state["breakeven_count"]
        total_gross_win += state["gross_win"]
        total_gross_loss += state["gross_loss"]
        total_net_win += state["net_win"]
        total_net_loss += state["net_loss"]
        total_fees += state["fees_paid"]

    total_closed = total_wins + total_losses
    overall = {
        "realized_pnl": total_realized,
        "win_count": total_wins,
        "loss_count": total_losses,
        "breakeven_count": total_breakevens,
        "win_rate": total_wins / total_closed if total_closed else None,
        "avg_win": total_net_win / total_wins if total_wins else None,
        "avg_loss": total_net_loss / total_losses if total_losses else None,
        "profit_factor": total_net_win / total_net_loss if total_net_loss else None,
        "gross_profit_factor": total_gross_win / total_gross_loss if total_gross_loss else None,
        "fees_paid": total_fees,
        "expectancy": total_realized / total_closed if total_closed else None,
    }
    # Version marker for consumers that might have cached an older,
    # long-only-biased trade_stats payload -- this function is always
    # recomputed fresh from trades.jsonl (no persistent cache exists in this
    # module), but downstream mirrors/exports should treat a missing or
    # different version as stale.
    return {
        "overall": overall,
        "by_symbol": symbol_stats,
        "trades": annotated,
        "trade_stats_version": "signed_weighted_average_v2",
    }


def _compute_unrealized_position_pnl(
    by_symbol: dict[str, dict[str, Any]],
    latest_prices: dict[str, float],
) -> dict[str, Any]:
    """Value every still-open symbol at the latest mark price (not last trade price).

    ``open_cost_basis`` folds in each symbol's unallocated entry-fee basis
    (``entry_fee_basis`` from ``compute_trade_stats``) so that ``market_value
    - open_cost_basis`` nets out to the same unrealized P&L implied by the
    equity identity below -- ``avg_cost`` itself is price-only (see
    ``compute_trade_stats``), so the fee has to be added back in here rather
    than assumed already baked into cost basis.

    A symbol with open quantity but no price in ``latest_prices`` is never
    defaulted to a $0 valuation -- that would silently manufacture a fake
    unrealized loss. Instead it is reported in ``stale_symbols`` and the
    caller treats the whole session as unreconciled until a fresh mark
    covers it.
    """
    open_cost_basis = 0.0
    open_market_value = 0.0
    open_position_count = 0
    stale_symbols: list[str] = []
    for code, stats in by_symbol.items():
        open_qty = stats["open_qty"]
        if not open_qty:
            continue
        open_position_count += 1
        symbol_cost_basis = open_qty * stats["avg_cost"] + stats["entry_fee_basis"]
        open_cost_basis += symbol_cost_basis
        price = latest_prices.get(code)
        if price is None:
            stale_symbols.append(code)
            continue
        open_market_value += open_qty * float(price)

    unrealized_pnl = None if stale_symbols else open_market_value - open_cost_basis
    return {
        "open_position_count": open_position_count,
        "open_cost_basis": open_cost_basis,
        "open_market_value": None if stale_symbols else open_market_value,
        "unrealized_pnl": unrealized_pnl,
        "stale_mark_symbols": stale_symbols,
    }


def compute_session_diagnostics(
    session_dir: Path,
    *,
    session: Optional[dict[str, Any]] = None,
    book: Optional[dict[str, Any]] = None,
    marks: Optional[list[dict[str, Any]]] = None,
    trades: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build a deterministic paper-trading diagnostic from the file ledger.

    ``session``/``book``/``marks``/``trades`` can be supplied directly (e.g.
    reconstructed in memory by ``session_reconciliation.py``) instead of
    read from ``session_dir`` on disk -- ``session_dir`` is still required
    for its ``.name`` and as the disk fallback for whichever of the four
    aren't supplied.
    """
    session = session if session is not None else _load_session(session_dir)
    book = book if book is not None else _load_book(session_dir)
    marks = marks if marks is not None else _read_jsonl(session_dir / "marks.jsonl")
    trades = trades if trades is not None else _read_jsonl(session_dir / "trades.jsonl")
    trade_stats = compute_trade_stats(trades)

    symbols = session["symbols"]
    n_symbols = len(symbols) or 1
    target_weight = 1.0 / n_symbols

    peak = None
    max_drawdown = 0.0
    weight_drift_sq_sum = 0.0
    weight_drift_samples = 0
    max_weight_drift = 0.0
    for mark in marks:
        equity = float(mark.get("equity", mark.get("current_equity", 0.0)))
        peak = equity if peak is None else max(peak, equity)
        drawdown = (equity - peak) / peak if peak else 0.0
        max_drawdown = min(max_drawdown, drawdown)

        # Tracking error vs. the equal-weight target -- a no-trade band that
        # saves fees but lets the basket drift materially off-target isn't
        # free; this quantifies that drift independent of whether it also
        # happened to help or hurt returns.
        position_values = mark.get("position_values") or {}
        if equity and all(code in position_values for code in symbols):
            deviations = [position_values[code] / equity - target_weight for code in symbols]
            weight_drift_sq_sum += sum(d * d for d in deviations)
            weight_drift_samples += len(deviations)
            max_weight_drift = max(max_weight_drift, max(abs(d) for d in deviations))

    tracking_error_rms = (
        (weight_drift_sq_sum / weight_drift_samples) ** 0.5 if weight_drift_samples else None
    )

    closed_trades = [t for t in trade_stats["trades"] if t.get("net_pnl") is not None]
    turnover = sum(float(t.get("notional", 0.0) or 0.0) for t in trades)
    initial_cash = float(session["initial_cash"])
    cash_remaining = float(book["cash_remaining"])
    realized_pnl = trade_stats["overall"]["realized_pnl"]
    total_fees = trade_stats["overall"]["fees_paid"]
    entry_fees = sum(float(t.get("fee_paid", 0.0) or 0.0) for t in trades if t.get("reason") == "entry")
    rebalance_fees = total_fees - entry_fees

    by_side: dict[str, dict[str, Any]] = {}
    by_reason: dict[str, dict[str, Any]] = {}
    for trade in closed_trades:
        for bucket, key in ((by_side, trade.get("side", "UNKNOWN")), (by_reason, trade.get("reason", "unknown"))):
            row = bucket.setdefault(str(key), {"count": 0, "net_pnl": 0.0, "wins": 0, "losses": 0})
            pnl = float(trade["net_pnl"])
            row["count"] += 1
            row["net_pnl"] += pnl
            if pnl > 0:
                row["wins"] += 1
            elif pnl < 0:
                row["losses"] += 1

    latest_mark = marks[-1] if marks else None
    latest_prices = latest_mark["prices"] if latest_mark else {}
    position = _compute_unrealized_position_pnl(trade_stats["by_symbol"], latest_prices)
    unrealized_pnl = position["unrealized_pnl"]

    # current_equity comes straight from the receipted mark (the same value
    # rebalance_if_due/_build_mark wrote to disk) rather than being
    # recomputed from cash_remaining + open_market_value here, so a stale or
    # missing per-symbol price can't quietly corrupt the headline equity
    # number even though it blocks the unrealized/reconciliation math below.
    current_equity = float(latest_mark.get("equity", latest_mark.get("current_equity", 0.0))) if latest_mark else cash_remaining
    net_portfolio_pnl = current_equity - initial_cash

    if unrealized_pnl is None:
        reconciliation_error = None
        reconciled = False
    else:
        reconciliation_error = current_equity - (initial_cash + realized_pnl + unrealized_pnl)
        tolerance = max(RECONCILIATION_ABS_TOLERANCE, abs(current_equity) * RECONCILIATION_REL_TOLERANCE)
        reconciled = abs(reconciliation_error) <= tolerance

    return {
        "session_id": session_dir.name,
        "strategy_name": session.get("strategy_type", STRATEGY_TYPE),
        "strategy_version": "paper_equal_weight_rebalance_v2_fee_aware",
        "signal_source": "none",
        "idimikang_used": False,
        "execution_permissions": "paper_only",
        "session": session,
        "book": book,
        "mark_count": len(marks),
        "trade_count": len(trades),
        "closed_trade_count": len(closed_trades),
        "metrics": {
            **trade_stats["overall"],
            "max_drawdown": max_drawdown,
            "turnover": turnover,
            "turnover_multiple": turnover / initial_cash if initial_cash else None,
            "average_fee_per_trade": trade_stats["overall"]["fees_paid"] / len(trades) if trades else None,
            "initial_cash": initial_cash,
            "cash_remaining": cash_remaining,
            "current_equity": current_equity,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_fees": total_fees,
            "entry_fees": entry_fees,
            "rebalance_fees": rebalance_fees,
            "net_portfolio_pnl": net_portfolio_pnl,
            "open_position_count": position["open_position_count"],
            "open_cost_basis": position["open_cost_basis"],
            "open_market_value": position["open_market_value"],
            "stale_mark_symbols": position["stale_mark_symbols"],
            "reconciliation_error": reconciliation_error,
            "reconciled": reconciled,
            "tracking_error_rms": tracking_error_rms,
            "max_weight_drift": max_weight_drift,
            "min_rebalance_notional": session.get("min_rebalance_notional", 0.0),
        },
        "by_symbol": trade_stats["by_symbol"],
        "by_side": by_side,
        "by_reason": by_reason,
        "closed_trades": closed_trades,
    }


RUNTIME_STALE_AFTER = timedelta(minutes=10)


def compute_session_status(session_dir: Path) -> dict[str, Any]:
    """Runtime/analysis/role metadata a frontend needs to NOT infer health
    from raw cash sign or assume a session is live just because its
    directory exists.

    ``runtime_status`` is derived from how recently marks.jsonl was appended
    to -- run_loop's mark_once/rebalance_if_due cycle writes a mark on every
    poll regardless of whether that poll rebalanced, so it's already a
    reliable heartbeat without a separate PID/heartbeat file.

    ``analysis_status`` prefers an on-disk reconciliation_report.json from
    session_reconciliation.py (batch_reconcile's output) when present, since
    that's the authoritative classification (VALID/RECONSTRUCTABLE/
    TAINTED_UNUSABLE); falls back to this session's own live `reconciled`
    flag when no reconstruction has been run for it yet.

    ``session_role`` distinguishes control/candidate/historical: schema-v1
    sessions (no accounting_schema_version stamped) predate the shadow-A/B
    concept entirely and are always "historical"; schema-v2 sessions are
    "candidate" if min_rebalance_notional > 0, else "control".
    """
    session = _load_session(session_dir)
    marks = _read_jsonl(session_dir / "marks.jsonl")

    # Prefer .heartbeat file for runtime status (more precise than marks age);
    # fall back to marks.jsonl age for legacy sessions without heartbeat.
    hb_path = session_dir / ".heartbeat"
    if hb_path.exists():
        runtime_status = _check_heartbeat(session_dir, stale_after_seconds=int(RUNTIME_STALE_AFTER.total_seconds()))
    elif not marks:
        runtime_status = "unknown"
    else:
        age = datetime.now(timezone.utc) - _parse_iso(marks[-1]["timestamp"])
        runtime_status = "running" if age < RUNTIME_STALE_AFTER else "stopped"

    schema_version = session.get("accounting_schema_version")
    accounting_status = session.get("accounting_status", "UNKNOWN")

    if schema_version is None:
        session_role = "historical"
    elif session.get("min_rebalance_notional", 0.0) > 0:
        session_role = "candidate"
    else:
        session_role = "control"

    hours = session.get("rebalance_interval_hours")
    if hours is None:
        regimen = "unknown"
    elif hours < 1:
        regimen = f"{round(hours * 60)}m"
    else:
        regimen = f"{hours:g}h"

    reconstructed_report_path = (
        Path(__file__).resolve().parent / "paper_sessions_reconstructed" / session_dir.name
        / "reconciliation_report.json"
    )
    if reconstructed_report_path.exists():
        report = json.loads(reconstructed_report_path.read_text(encoding="utf-8"))
        status_map = {"VALID": "valid", "RECONSTRUCTABLE": "reconstructed", "TAINTED_UNUSABLE": "tainted"}
        analysis_status = status_map.get(report.get("status"), "invalid")
    else:
        diagnostics = compute_session_diagnostics(session_dir)
        analysis_status = "valid" if diagnostics["metrics"]["reconciled"] else "invalid"

    return {
        "runtime_status": runtime_status,
        "analysis_status": analysis_status,
        "accounting_status": accounting_status,
        "accounting_schema_version": schema_version,
        "session_role": session_role,
        "regimen": regimen,
        "active": runtime_status == "running",
    }


def build_shadow_comparison(sessions_dir: Path) -> list[dict[str, Any]]:
    """Pair up control/candidate schema-v2 sessions by regimen and compute deltas.

    Only sessions with session_role in (control, candidate) participate --
    historical (schema-v1) sessions are never paired, since they predate
    the concept of a threshold arm entirely.
    """
    by_regimen: dict[str, dict[str, dict[str, Any]]] = {}
    for d in sorted(p for p in sessions_dir.iterdir() if p.is_dir() and (p / "session.json").exists()):
        status = compute_session_status(d)
        if status["session_role"] not in ("control", "candidate"):
            continue
        diagnostics = compute_session_diagnostics(d)
        m = diagnostics["metrics"]
        row = {
            "session_id": d.name,
            **status,
            "net_return": m["net_portfolio_pnl"] / m["initial_cash"] if m["initial_cash"] else None,
            "trade_count": diagnostics["trade_count"],
            "rebalance_fees": m["rebalance_fees"],
            "total_fees": m["total_fees"],
            "turnover": m["turnover"],
            "max_drawdown": m["max_drawdown"],
            "tracking_error_rms": m["tracking_error_rms"],
            "max_weight_drift": m["max_weight_drift"],
            "min_rebalance_notional": m["min_rebalance_notional"],
            "reconciled": m["reconciled"],
        }
        by_regimen.setdefault(status["regimen"], {})[status["session_role"]] = row

    comparisons = []
    for regimen, arms in sorted(by_regimen.items()):
        control = arms.get("control")
        candidate = arms.get("candidate")
        delta = None
        if control and candidate and control["net_return"] is not None and candidate["net_return"] is not None:
            delta = {
                "net_return": candidate["net_return"] - control["net_return"],
                "total_fees": candidate["total_fees"] - control["total_fees"],
                "trade_count": candidate["trade_count"] - control["trade_count"],
                "turnover": candidate["turnover"] - control["turnover"],
                "max_drawdown": candidate["max_drawdown"] - control["max_drawdown"],
            }
        comparisons.append({
            "regimen": regimen,
            "control_session_id": control["session_id"] if control else None,
            "candidate_session_id": candidate["session_id"] if candidate else None,
            "control": control,
            "candidate": candidate,
            "delta": delta,
        })
    return comparisons


def export_closed_trade_diagnostics_csv(session_dir: Path, output_path: Path) -> Path:
    """Export completed paper-trade diagnostics to CSV for external review."""
    diagnostics = compute_session_diagnostics(session_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "trade_id",
        "symbol",
        "side",
        "entry_time",
        "entry_price",
        "exit_time",
        "exit_price",
        "quantity",
        "gross_pnl",
        "fees",
        "funding",
        "slippage",
        "net_pnl",
        "exit_reason",
        "strategy_name",
        "strategy_version",
        "signal_source",
        "idimikang_event_id",
        "idimikang_score",
        "confidence",
        "market_regime",
        "stop_distance",
        "take_profit_distance",
        "maximum_favorable_excursion",
        "maximum_adverse_excursion",
        "realized_pnl",
        "unrealized_pnl",
        "net_portfolio_pnl",
        "current_equity",
        "initial_cash",
        "open_cost_basis",
        "open_market_value",
        "reconciliation_error",
        "reconciled",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, restval="")
        writer.writeheader()
        for index, trade in enumerate(diagnostics["closed_trades"], start=1):
            writer.writerow({
                "trade_id": f"{session_dir.name}:{index}",
                "symbol": trade["symbol"],
                "side": trade["side"],
                "entry_time": trade.get("entry_time") or "",
                "entry_price": trade.get("entry_price") or "",
                "exit_time": trade["timestamp"],
                "exit_price": trade["price"],
                "quantity": trade["qty"],
                "gross_pnl": trade.get("gross_pnl"),
                "fees": trade.get("total_fees"),
                "funding": 0.0,
                "slippage": 0.0,
                "net_pnl": trade.get("net_pnl"),
                "exit_reason": trade.get("reason"),
                "strategy_name": diagnostics["strategy_name"],
                "strategy_version": diagnostics["strategy_version"],
                "signal_source": diagnostics["signal_source"],
                "idimikang_event_id": "",
                "idimikang_score": "",
                "confidence": "",
                "market_regime": "",
                "stop_distance": "",
                "take_profit_distance": "",
                "maximum_favorable_excursion": "",
                "maximum_adverse_excursion": "",
            })
        metrics = diagnostics["metrics"]
        writer.writerow({
            "trade_id": f"{session_dir.name}:SESSION_SUMMARY",
            "realized_pnl": metrics["realized_pnl"],
            "unrealized_pnl": metrics["unrealized_pnl"],
            "net_portfolio_pnl": metrics["net_portfolio_pnl"],
            "current_equity": metrics["current_equity"],
            "initial_cash": metrics["initial_cash"],
            "open_cost_basis": metrics["open_cost_basis"],
            "open_market_value": metrics["open_market_value"],
            "reconciliation_error": metrics["reconciliation_error"],
            "reconciled": metrics["reconciled"],
        })
    return output_path


def status(session_dir: Path) -> dict[str, Any]:
    """Return session config, current book, latest mark, and recent trades -- straight from disk."""
    session = _load_session(session_dir)
    book = _load_book(session_dir)
    marks = _read_jsonl(session_dir / "marks.jsonl")
    trades = _read_jsonl(session_dir / "trades.jsonl")
    trade_stats = compute_trade_stats(trades)
    return {
        "session": session,
        "book": book,
        "mark_count": len(marks),
        "latest_mark": marks[-1] if marks else None,
        "trade_count": len(trades),
        "recent_trades": trade_stats["trades"][-20:],
        "trade_stats": {"overall": trade_stats["overall"], "by_symbol": trade_stats["by_symbol"]},
    }


def run_loop(session_dir: Path, poll_seconds: int, until_iso: Optional[str]) -> None:
    """Loop marking to market and rebalancing-if-due every ``poll_seconds``.

    Open-ended by default (``until_iso=None``) -- meant to accumulate trades
    across days, not stop at end of day. Meant to run as a detached
    background process: each mark/rebalance is a complete, receipted write,
    so killing this loop at any point leaves a valid, readable session on
    disk rather than a half-written one.
    """
    until = _parse_iso(until_iso) if until_iso else None
    # Check for crash recovery on entry
    recovery_state = _recover_from_journal(session_dir)
    if recovery_state is not None:
        logger.warning("session %s: incomplete journal detected, restoring last checkpoint", session_dir.name)
        restored_book = {"positions": recovery_state.get("positions", {}), "cash_remaining": recovery_state.get("cash", 0.0), "last_rebalance_time": _now_iso()}
        receipted_write(session_dir / "book.json", json.dumps(restored_book, indent=2))
        _append_journal(session_dir, {"type": "recovered"})
    while until is None or datetime.now(timezone.utc) < until:
        try:
            result = rebalance_if_due(session_dir)
            if result is not None:
                print(json.dumps({"event": "rebalance", **result}), flush=True)
            else:
                mark = mark_once(session_dir)
                print(json.dumps({"event": "mark", **mark}), flush=True)
        except Exception as exc:  # noqa: BLE001 - transient fetch failure, keep looping
            print(json.dumps({"event": "poll_error", "error": str(exc), "tb": traceback.format_exc().splitlines()[-3:], "timestamp": _now_iso()}), flush=True)
        _update_heartbeat(session_dir)
        time.sleep(poll_seconds)
    print(json.dumps({"event": "session_complete", "timestamp": _now_iso()}), flush=True)


def _assert_shared_symbol_set(sessions_by_dir: dict[Path, dict[str, Any]], caller: str) -> list[str]:
    """Verify every session in a pair/group shares the same symbol set.

    A shared quote fetch only means something if every session is buying
    the same instruments -- otherwise "the same snapshot" is meaningless.
    Raises before any fetch or write happens, so a misconfigured group
    fails fast instead of silently pairing on a partial symbol overlap.

    Returns:
        The reference symbol list (order taken from the first session), used
        to drive the single shared price fetch.
    """
    dirs = list(sessions_by_dir)
    reference_symbols = sessions_by_dir[dirs[0]]["symbols"]
    reference_set = set(reference_symbols)
    for d in dirs[1:]:
        if set(sessions_by_dir[d]["symbols"]) != reference_set:
            raise ValueError(
                f"{caller} requires every session to share the same symbol set; "
                f"{d.name} has {sorted(sessions_by_dir[d]['symbols'])}, expected "
                f"{sorted(reference_set)}"
            )
    return reference_symbols


def start_paired_sessions(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Start a group of sessions (e.g. a control/candidate shadow A/B) on one shared entry quote.

    Two independent ``start_session`` calls a few seconds apart let quote-
    timing noise into the entry basket before either policy has traded --
    this fetches once and stamps once, then hands every session the
    identical entry_prices and entry_time instead.

    Args:
        specs: One dict per session, each with the keyword args
            ``start_session`` takes: ``session_dir``, ``symbols``,
            ``initial_cash``, ``rebalance_interval_hours``, and optionally
            ``fee_rate`` / ``min_rebalance_notional``. Every spec must list
            the same ``symbols`` (order-independent) -- that shared universe
            is what "paired" means here.

    Returns:
        The list of started ``session`` dicts, in the same order as ``specs``.
    """
    if not specs:
        raise ValueError("start_paired_sessions requires at least one spec")
    symbols = _assert_shared_symbol_set(
        {spec["session_dir"]: {"symbols": spec["symbols"]} for spec in specs},
        "start_paired_sessions",
    )
    entry_prices = fetch_last_prices(symbols)
    entry_time = _now_iso()
    return [
        start_session(
            spec["session_dir"],
            spec["symbols"],
            spec["initial_cash"],
            spec["rebalance_interval_hours"],
            fee_rate=spec.get("fee_rate", 0.0),
            min_rebalance_notional=spec.get("min_rebalance_notional", 0.0),
            entry_prices=entry_prices,
            entry_time=entry_time,
            risk_config=spec.get("risk_config"),
        )
        for spec in specs
    ]


def run_paired_loop(session_dirs: list[Path], poll_seconds: int, until_iso: Optional[str]) -> None:
    """Like ``run_loop``, but drives a group of sessions off one shared price fetch per tick.

    Independent ``run_loop`` processes -- one per session -- each fetch
    their own quote and stamp their own timestamp, so a control/candidate
    pair ends up comparing policy effect confounded with quote-timing noise
    (two API calls and two clock reads, whatever interval apart). This
    fetches once and stamps once per tick for the group's shared symbol
    universe, feeding the identical prices dict and timestamp into every
    session's ``rebalance_if_due`` / ``mark_once`` call that tick, so any
    measured difference between sessions is attributable to policy, not to
    which one happened to see a fresher quote or get written a moment later.

    Same crash-safety property as ``run_loop``: every mark/rebalance is a
    complete, receipted write per session, so killing this loop at any
    point leaves every session in the group valid on disk.
    """
    sessions_by_dir = {d: _load_session(d) for d in session_dirs}
    symbols = _assert_shared_symbol_set(sessions_by_dir, "run_paired_loop")

    until = _parse_iso(until_iso) if until_iso else None
    while until is None or datetime.now(timezone.utc) < until:
        try:
            prices = fetch_last_prices(symbols)
            tick_now = _now_iso()
            for session_dir in session_dirs:
                try:
                    result = rebalance_if_due(session_dir, prices=prices, now=tick_now)
                    if result is not None:
                        print(json.dumps({"event": "rebalance", "session": session_dir.name, **result}), flush=True)
                    else:
                        mark = mark_once(session_dir, prices=prices, now=tick_now)
                        print(json.dumps({"event": "mark", "session": session_dir.name, **mark}), flush=True)
                except Exception as exc:  # noqa: BLE001 - isolate account failures
                    print(json.dumps({
                        "event": "session_poll_error",
                        "session": session_dir.name,
                        "error": str(exc),
                        "timestamp": tick_now,
                    }), flush=True)
                finally:
                    _update_heartbeat(session_dir)
        except Exception as exc:  # noqa: BLE001 - transient fetch failure, keep looping
            print(json.dumps({"event": "poll_error", "error": str(exc), "timestamp": _now_iso()}), flush=True)
        time.sleep(poll_seconds)
    print(json.dumps({"event": "session_complete", "timestamp": _now_iso()}), flush=True)


# ---------------------------------------------------------------------------
# Funding-rate z-score strategy
# ---------------------------------------------------------------------------

def _fetch_funding_rates(symbols: list[str]) -> dict[str, float]:
    """Fetch the current funding rate for each symbol via Binance's public API."""
    exchange = _get_cached_futures_exchange()
    rates: dict[str, float] = {}
    for code in symbols:
        ccxt_sym = _ccxt_symbol(code)
        try:
            fr = exchange.fetch_funding_rate(ccxt_sym)
            rates[code] = float(fr.get("fundingRate", 0.0))
        except Exception:
            rates[code] = 0.0
    return rates


def _fetch_funding_rate_history_ccxt(
    symbol: str, limit: int = 500,
) -> list[dict[str, Any]]:
    """Fetch recent funding-rate history for one symbol via ccxt."""
    exchange = _get_cached_futures_exchange()
    ccxt_sym = _ccxt_symbol(symbol)
    try:
        raw = exchange.fetch_funding_rate_history(ccxt_sym, limit=limit)
        return [
            {"timestamp": r.get("timestamp"), "funding_rate": float(r.get("fundingRate", 0.0))}
            for r in raw
            if r.get("timestamp") is not None
        ]
    except Exception:
        return []


def _compute_funding_zscore(
    history: list[dict[str, Any]],
    current_rate: float,
    window: int,
) -> float:
    """Compute a rolling z-score of the current funding rate vs history."""
    rates = [r["funding_rate"] for r in history[-window:]]
    if len(rates) < max(10, window // 3):
        return 0.0
    mean = sum(rates) / len(rates)
    variance = sum((r - mean) ** 2 for r in rates) / len(rates)
    std = variance ** 0.5
    if std < 1e-12:
        return 0.0
    return (current_rate - mean) / std


def start_funding_session(
    session_dir: Path,
    symbols: list[str],
    initial_cash: float,
    *,
    z_window: int = 120,
    entry_z: float = 1.5,
    exit_z: float = 0.5,
    fee_rate: float = 0.0005,
    slippage_rate: float = 0.0005,
    max_position_pct: float = 0.25,
    poll_seconds: int = 300,
    risk_config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Start a funding-rate z-score paper-trading session.

    Unlike the equal-weight rebalance strategy, this session starts in 100% cash
    and only takes positions when the funding-rate z-score signal fires.
    Positions can be long (negative funding → shorts pay longs) or short
    (positive funding → longs pay shorts).
    """
    if session_dir.exists():
        raise FileExistsError(f"session_dir already exists: {session_dir}")
    session_dir.mkdir(parents=True)

    entry_time = _now_iso()
    session = {
        "strategy_type": FUNDING_ZSCORE_STRATEGY,
        "symbols": symbols,
        "initial_cash": initial_cash,
        "entry_time": entry_time,
        "entry_prices": {},
        "rebalance_interval_hours": poll_seconds / 3600.0,
        "fee_rate": fee_rate,
        "slippage_rate": slippage_rate,
        "source": "binance",
        "price_kind": "live_ticker_last",
        "fees_modeled": fee_rate > 0,
        "slippage_modeled": slippage_rate > 0,
        "cash_accounting_note": None,
        "accounting_schema_version": ACCOUNTING_SCHEMA_VERSION,
        "accounting_status": "OK",
        "min_rebalance_notional": 0.0,
        "z_window": z_window,
        "entry_z": entry_z,
        "exit_z": exit_z,
        "max_position_pct": max_position_pct,
        "poll_seconds": poll_seconds,
        "risk_config": risk_config if risk_config is not None else dict(DEFAULT_RISK_CONFIG),
    }
    receipted_write(session_dir / "session.json", json.dumps(session, indent=2))
    _mirror_session_to_store(session_dir.name, session)

    book = {"positions": {}, "cash_remaining": initial_cash, "last_rebalance_time": entry_time, "position_metadata": {}}
    receipted_write(session_dir / "book.json", json.dumps(book, indent=2))

    prices = fetch_last_prices(symbols)
    entry_mark = _build_mark(session, book, prices, now=entry_time)
    _append_jsonl(session_dir / "marks.jsonl", entry_mark)
    _mirror_mark_to_store(session_dir.name, entry_mark)
    return session


def funding_rebalance_if_due(
    session_dir: Path,
    *,
    prices: Optional[dict[str, float]] = None,
    funding_rates: Optional[dict[str, float]] = None,
    now: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Check funding-rate z-score signals and trade if positions need to change.

    Returns executed trades + mark, or None if not due yet.
    """
    session = _load_session(session_dir)
    if session.get("accounting_status") == "ACCOUNTING_ERROR":
        raise RuntimeError(
            f"session {session_dir.name} is paused: accounting invariant violated."
        )
    book = _load_book(session_dir)
    last_rebalance = _parse_iso(book["last_rebalance_time"])
    poll_interval = timedelta(seconds=session.get("poll_seconds", 300))
    wall_clock_now = datetime.now(timezone.utc)
    timestamp = now if now is not None else _now_iso()

    symbols = session["symbols"]
    prices = dict(prices) if prices is not None else fetch_last_prices(symbols)

    # Risk checks run on every call, even when poll interval hasn't elapsed.
    risk_exit_intents = _check_risk_exits(session, book, prices, timestamp, session_dir)
    risk_exits = _execute_risk_exit_intents(session, book, session_dir, risk_exit_intents, timestamp)
    if risk_exits:
        receipted_write(session_dir / "book.json", json.dumps(book, indent=2))

    if wall_clock_now - last_rebalance < poll_interval:
        if risk_exits:
            funding_rates = dict(funding_rates) if funding_rates is not None else _fetch_funding_rates(symbols)
            mark = _build_mark(session, book, prices, now=timestamp)
            mark["funding_rates"] = funding_rates
            _append_jsonl(session_dir / "marks.jsonl", mark)
            _mirror_mark_to_store(session_dir.name, mark)
            return {"trades": risk_exits, "mark": mark}
        return None

    funding_rates = dict(funding_rates) if funding_rates is not None else _fetch_funding_rates(symbols)

    z_window = session.get("z_window", 120)
    entry_z = session.get("entry_z", 1.5)
    exit_z = session.get("exit_z", 0.5)
    max_position_pct = session.get("max_position_pct", 0.25)

    equity = float(book["cash_remaining"]) + sum(
        book["positions"].get(s, 0.0) * prices[s] for s in symbols
    )

    risk = session.get("risk_config", DEFAULT_RISK_CONFIG)
    leverage = risk.get("leverage", 1.0)
    margin_mode = risk.get("margin_mode", "cross")
    liq_buffer = risk.get("liquidation_buffer_pct", 0.10)
    fixed_margin = risk.get("fixed_margin_per_trade", 0.0)

    executed: list[dict[str, Any]] = []

    for sym in symbols:
        history = _fetch_funding_rate_history_ccxt(sym, limit=z_window + 50)
        z = _compute_funding_zscore(history, funding_rates.get(sym, 0.0), z_window)
        price = prices[sym]
        current_qty = book["positions"].get(sym, 0.0)

        if current_qty == 0.0:
            # No position — check for entry signals. Same shared executor
            # for both directions: futures_ledger.open_position reserves
            # margin (target_notional / leverage) instead of debiting/
            # crediting full notional as cash.
            side = None
            if z <= -entry_z:
                side = LedgerSide.LONG  # negative funding means shorts pay longs
            elif z >= entry_z:
                side = LedgerSide.SHORT  # positive funding means longs pay shorts
            if side is not None:
                target_notional = fixed_margin * leverage if fixed_margin > 0 else equity * max_position_pct
                try:
                    trade = _execute_open_position(
                        session, book, sym, side, target_notional, price, timestamp,
                        leverage, margin_mode, liq_buffer,
                        reason=f"funding_z_{'long' if side is LedgerSide.LONG else 'short'} z={z:.2f}",
                    )
                except ValueError:
                    continue  # not enough cash for this trade -- skip, unchanged from prior behavior
                trade["funding_rate"] = funding_rates.get(sym, 0.0)
                trade["z_score"] = z
                executed.append(trade)
                _append_jsonl(session_dir / "trades.jsonl", trade)
                _mirror_trade_to_store(session_dir.name, trade)

        else:
            # Have a position — check for exit signal. Same executor as
            # every risk-exit reason (take_profit/stop_loss/trailing_stop/
            # max_hold_expired/liquidation): _execute_close_intent.
            if abs(z) < exit_z:
                intent = CloseIntent(symbol=sym, quantity=abs(current_qty), reason=f"funding_z_exit z={z:.2f}", mark_price=price)
                trade = _execute_close_intent(session, book, session_dir, intent, timestamp)
                trade["funding_rate"] = funding_rates.get(sym, 0.0)
                trade["z_score"] = z
                executed.append(trade)
                _append_jsonl(session_dir / "trades.jsonl", trade)
                _mirror_trade_to_store(session_dir.name, trade)

    if abs(book["cash_remaining"]) < 1e-9:
        book["cash_remaining"] = 0.0

    book["last_rebalance_time"] = timestamp
    receipted_write(session_dir / "book.json", json.dumps(book, indent=2))

    mark = _build_mark(session, book, prices, now=timestamp)
    mark["funding_rates"] = funding_rates
    _append_jsonl(session_dir / "marks.jsonl", mark)
    _mirror_mark_to_store(session_dir.name, mark)

    all_trades = risk_exits + executed
    return {"trades": all_trades, "mark": mark}


def run_funding_loop(
    session_dir: Path, poll_seconds: int, until_iso: Optional[str],
) -> None:
    """Polling loop for funding-rate z-score sessions."""
    until = _parse_iso(until_iso) if until_iso else None
    while until is None or datetime.now(timezone.utc) < until:
        try:
            result = funding_rebalance_if_due(session_dir)
            if result is not None:
                print(json.dumps({"event": "funding_rebalance", **result}), flush=True)
            else:
                mark = mark_once(session_dir)
                print(json.dumps({"event": "mark", **mark}), flush=True)
        except Exception as exc:
            print(json.dumps({"event": "poll_error", "error": str(exc), "timestamp": _now_iso()}), flush=True)
        _update_heartbeat(session_dir)
        time.sleep(poll_seconds)
    print(json.dumps({"event": "session_complete", "timestamp": _now_iso()}), flush=True)


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--session-dir", required=True)
    p_start.add_argument("--symbols", required=True, help="comma-separated, e.g. BTC-USDT,ETH-USDT,SOL-USDT")
    p_start.add_argument("--cash", type=float, required=True)
    p_start.add_argument("--rebalance-hours", type=float, default=4.0)
    p_start.add_argument("--fee-rate", type=float, default=0.0, help="e.g. 0.001 for 0.1pct taker fee per trade")
    p_start.add_argument(
        "--min-notional", type=float, default=10.0,
        help="suppress rebalance trades below this $ delta (default 10.0; 0.0 = control)",
    )
    # Risk management args
    p_start.add_argument("--take-profit-pct", type=float, default=None, help="e.g. 0.05 = close at +5pct from entry")
    p_start.add_argument("--stop-loss-pct", type=float, default=None, help="e.g. 0.03 = close at -3pct from entry")
    p_start.add_argument("--trailing-stop-pct", type=float, default=None, help="e.g. 0.02 = trail 2pct from high-water mark")
    p_start.add_argument("--max-hold-hours", type=float, default=None, help="force-close positions after N hours")
    p_start.add_argument("--leverage", type=float, default=1.0, help="leverage multiplier (1x=spot, 2x-10x=margined)")
    p_start.add_argument("--margin-mode", choices=["cross", "isolated"], default="isolated")
    p_start.add_argument("--liquidation-buffer-pct", type=float, default=0.10, help="safety margin for liquidation calc")
    p_start.add_argument("--fixed-margin", type=float, default=0.0, help="fixed $ margin per position (0 = use percentage sizing)")

    p_run = sub.add_parser("run")
    p_run.add_argument("--session-dir", required=True)
    p_run.add_argument("--poll-seconds", type=int, default=300)
    p_run.add_argument("--until", default=None, help="ISO 8601 UTC timestamp to stop at; omit to run open-ended")

    p_start_paired = sub.add_parser(
        "start-paired",
        help="Start a control/candidate (or any N-way) shadow group on one shared entry quote.",
    )
    p_start_paired.add_argument("--session-dirs", required=True, help="comma-separated session directories")
    p_start_paired.add_argument("--symbols", required=True, help="comma-separated, shared across every session in the group")
    p_start_paired.add_argument("--cash", type=float, required=True)
    p_start_paired.add_argument("--rebalance-hours", type=float, default=4.0)
    p_start_paired.add_argument("--fee-rate", type=float, default=0.0)
    p_start_paired.add_argument(
        "--min-notionals", required=True,
        help="comma-separated, one per --session-dirs entry, e.g. 0.0,10.0 for control,candidate10",
    )

    p_run_paired = sub.add_parser(
        "run-paired",
        help="Run a control/candidate (or any N-way) shadow group off one shared price fetch per tick.",
    )
    p_run_paired.add_argument("--session-dirs", required=True, help="comma-separated session directories")
    p_run_paired.add_argument("--poll-seconds", type=int, default=300)
    p_run_paired.add_argument("--until", default=None, help="ISO 8601 UTC timestamp to stop at; omit to run open-ended")

    p_status = sub.add_parser("status")
    p_status.add_argument("--session-dir", required=True)

    p_mark = sub.add_parser("mark")
    p_mark.add_argument("--session-dir", required=True)

    p_rebalance = sub.add_parser("rebalance")
    p_rebalance.add_argument("--session-dir", required=True)
    p_rebalance.add_argument("--force", action="store_true")

    p_diag = sub.add_parser("diagnostics")
    p_diag.add_argument("--session-dir", required=True)
    p_diag.add_argument("--csv-out", default=None, help="Optional CSV path for closed-trade diagnostics")

    p_start_funding = sub.add_parser("start-funding", help="Start a funding-rate z-score session")
    p_start_funding.add_argument("--session-dir", required=True)
    p_start_funding.add_argument("--symbols", required=True)
    p_start_funding.add_argument("--cash", type=float, required=True)
    p_start_funding.add_argument("--z-window", type=int, default=120)
    p_start_funding.add_argument("--entry-z", type=float, default=1.5)
    p_start_funding.add_argument("--exit-z", type=float, default=0.5)
    p_start_funding.add_argument("--fee-rate", type=float, default=0.0005)
    p_start_funding.add_argument("--slippage", type=float, default=0.0005)
    p_start_funding.add_argument("--max-position-pct", type=float, default=0.25)
    p_start_funding.add_argument("--poll-seconds", type=int, default=300)
    # Risk management args
    p_start_funding.add_argument("--take-profit-pct", type=float, default=None, help="e.g. 0.05 = close at +5pct from entry")
    p_start_funding.add_argument("--stop-loss-pct", type=float, default=None, help="e.g. 0.03 = close at -3pct from entry")
    p_start_funding.add_argument("--trailing-stop-pct", type=float, default=None, help="e.g. 0.02 = trail 2pct from high-water mark")
    p_start_funding.add_argument("--max-hold-hours", type=float, default=None, help="force-close positions after N hours")
    p_start_funding.add_argument("--leverage", type=float, default=1.0, help="leverage multiplier (1x=spot, 2x-10x=margined)")
    p_start_funding.add_argument("--margin-mode", choices=["cross", "isolated"], default="isolated")
    p_start_funding.add_argument("--liquidation-buffer-pct", type=float, default=0.10, help="safety margin for liquidation calc")
    p_start_funding.add_argument("--fixed-margin", type=float, default=0.0, help="fixed $ margin per position (0 = use percentage sizing)")

    p_run_funding = sub.add_parser("run-funding", help="Run a funding-rate z-score session loop")
    p_run_funding.add_argument("--session-dir", required=True)
    p_run_funding.add_argument("--poll-seconds", type=int, default=300)
    p_run_funding.add_argument("--until", default=None)

    args = parser.parse_args()
    if args.command in ("start-paired", "run-paired"):
        session_dirs = [Path(d.strip()) for d in args.session_dirs.split(",") if d.strip()]
    else:
        session_dir = Path(args.session_dir)

    if args.command == "start":
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        risk_cfg = _default_risk_config(
            take_profit_pct=args.take_profit_pct,
            stop_loss_pct=args.stop_loss_pct,
            trailing_stop_pct=args.trailing_stop_pct,
            max_hold_hours=args.max_hold_hours,
            leverage=args.leverage,
            margin_mode=args.margin_mode,
            liquidation_buffer_pct=args.liquidation_buffer_pct,
            fixed_margin_per_trade=args.fixed_margin,
        )
        result = start_session(
            session_dir, symbols, args.cash, args.rebalance_hours, args.fee_rate, args.min_notional,
            risk_config=risk_cfg,
        )
        print(json.dumps(result, indent=2))
    elif args.command == "run":
        run_loop(session_dir, args.poll_seconds, args.until)
    elif args.command == "start-paired":
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        min_notionals = [float(v.strip()) for v in args.min_notionals.split(",") if v.strip()]
        if len(min_notionals) != len(session_dirs):
            raise SystemExit(
                f"--min-notionals has {len(min_notionals)} values but --session-dirs has "
                f"{len(session_dirs)}; need exactly one per session"
            )
        specs = [
            {
                "session_dir": d,
                "symbols": symbols,
                "initial_cash": args.cash,
                "rebalance_interval_hours": args.rebalance_hours,
                "fee_rate": args.fee_rate,
                "min_rebalance_notional": min_notional,
            }
            for d, min_notional in zip(session_dirs, min_notionals)
        ]
        results = start_paired_sessions(specs)
        print(json.dumps(results, indent=2))
    elif args.command == "run-paired":
        run_paired_loop(session_dirs, args.poll_seconds, args.until)
    elif args.command == "status":
        print(json.dumps(status(session_dir), indent=2))
    elif args.command == "mark":
        print(json.dumps(mark_once(session_dir), indent=2))
    elif args.command == "rebalance":
        result = rebalance_if_due(session_dir, force=args.force)
        print(json.dumps(result, indent=2) if result else json.dumps({"event": "not_due"}))
    elif args.command == "start-funding":
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        risk_cfg = _default_risk_config(
            take_profit_pct=args.take_profit_pct,
            stop_loss_pct=args.stop_loss_pct,
            trailing_stop_pct=args.trailing_stop_pct,
            max_hold_hours=args.max_hold_hours,
            leverage=args.leverage,
            margin_mode=args.margin_mode,
            liquidation_buffer_pct=args.liquidation_buffer_pct,
            fixed_margin_per_trade=args.fixed_margin,
        )
        result = start_funding_session(
            session_dir, symbols, args.cash,
            z_window=args.z_window, entry_z=args.entry_z, exit_z=args.exit_z,
            fee_rate=args.fee_rate, slippage_rate=args.slippage,
            max_position_pct=args.max_position_pct, poll_seconds=args.poll_seconds,
            risk_config=risk_cfg,
        )
        print(json.dumps(result, indent=2))
    elif args.command == "run-funding":
        run_funding_loop(session_dir, args.poll_seconds, args.until)
    elif args.command == "diagnostics":
        result = compute_session_diagnostics(session_dir)
        if args.csv_out:
            export_closed_trade_diagnostics_csv(session_dir, Path(args.csv_out))
            result["csv_out"] = args.csv_out
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _cli()
