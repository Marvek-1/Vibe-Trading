#!/usr/bin/env python3
"""MoStar Futures Paper Engine.

Paper-only USDT-margined futures simulator with explicit market provenance:
- isolated margin only (cross margin is rejected until it has its own
  shared-pool accounting model)
- 5x / 10x leverage
- $20-$100 margin per trade
- immutable closed-trade ledger
- TP / SL / trailing stop / max hold
- maker/taker fees
- optional funding rate application, restart-idempotent
- liquidation checks
- JSONL receipts and account reconciliation
- validate-before-persist mutations; a cross-process writer lease

No real orders are placed.
"""

from __future__ import annotations

import atexit
import copy
import hashlib
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

Side = Literal["long", "short"]
MarginMode = Literal["isolated", "cross"]
OrderType = Literal["maker", "taker"]
MarketSource = Literal["okx", "binance", "bybit", "gate"]

ALLOWED_LEVERAGE = {5, 10}
MIN_MARGIN = 1.0
MAX_MARGIN = 100.0
DEFAULT_MARGIN = 50.0
DEFAULT_LEVERAGE = 5
DEFAULT_MARGIN_MODE: MarginMode = "isolated"

BINANCE_FAPI = "https://fapi.binance.com"
EPSILON = 1e-9


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(data, encoding="utf-8")
    temp.replace(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(digest, encoding="utf-8")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(digest, encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def request_json(path: str, params: Optional[dict[str, Any]] = None, timeout: int = 15) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = f"{BINANCE_FAPI}{path}"
    if query:
        url += "?" + query
    req = urllib.request.Request(url, headers={"User-Agent": "MoStar-Futures-Paper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_mark_price(symbol: str) -> float:
    payload = request_json("/fapi/v1/premiumIndex", {"symbol": normalize_symbol(symbol)})
    return float(payload["markPrice"])


def _public_json(url: str, params: Optional[dict[str, Any]] = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    req = urllib.request.Request(
        f"{url}?{query}" if query else url,
        headers={"User-Agent": "MoStar-Futures-Paper/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _provider_symbol(symbol: str, source: MarketSource) -> str:
    normalized = normalize_symbol(symbol)
    base = normalized.removesuffix("USDT")
    if source == "okx":
        return f"{base}-USDT-SWAP"
    if source == "gate":
        return f"{base}_USDT"
    return normalized


def fetch_funding_rate(symbol: str, *, source: MarketSource = "binance") -> float:
    provider_symbol = _provider_symbol(symbol, source)
    if source == "okx":
        payload = _public_json(
            "https://www.okx.com/api/v5/public/funding-rate",
            {"instId": provider_symbol},
        )
        rows = payload.get("data", [])
        if not rows:
            raise RuntimeError(f"No OKX funding rate for {symbol}")
        return float(rows[0]["fundingRate"])
    if source == "bybit":
        payload = _public_json(
            "https://api.bybit.com/v5/market/tickers",
            {"category": "linear", "symbol": provider_symbol},
        )
        rows = payload.get("result", {}).get("list", [])
        if not rows:
            raise RuntimeError(f"No Bybit funding rate for {symbol}")
        return float(rows[0]["fundingRate"])
    if source == "gate":
        payload = _public_json(
            f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{provider_symbol}"
        )
        return float(payload["funding_rate"])
    payload = request_json("/fapi/v1/premiumIndex", {"symbol": provider_symbol})
    return float(payload["lastFundingRate"])


def fetch_funding_history(
    symbol: str,
    limit: int,
    *,
    source: MarketSource = "binance",
) -> list[float]:
    provider_symbol = _provider_symbol(symbol, source)
    if source == "okx":
        payload = _public_json(
            "https://www.okx.com/api/v5/public/funding-rate-history",
            {"instId": provider_symbol, "limit": min(limit, 100)},
        )
        return [float(row["fundingRate"]) for row in payload.get("data", []) if row.get("fundingRate")]
    if source == "bybit":
        payload = _public_json(
            "https://api.bybit.com/v5/market/funding/history",
            {"category": "linear", "symbol": provider_symbol, "limit": min(limit, 200)},
        )
        rows = payload.get("result", {}).get("list", [])
        return [float(row["fundingRate"]) for row in rows if row.get("fundingRate")]
    if source == "gate":
        payload = _public_json(
            "https://api.gateio.ws/api/v4/futures/usdt/funding_rate",
            {"contract": provider_symbol, "limit": min(limit, 1000)},
        )
        return [float(row["r"]) for row in payload if row.get("r")]
    payload = request_json(
        "/fapi/v1/fundingRate",
        {"symbol": provider_symbol, "limit": min(limit, 1000)},
    )
    return [float(row["fundingRate"]) for row in payload if row.get("fundingRate")]


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace("-", "").replace("/", "").replace("_", "")


class WriterLockError(RuntimeError):
    """Raised when a second process tries to open the same session for writing."""


class InvariantViolation(AssertionError):
    """Raised when a proposed mutation would leave the ledger inconsistent."""


@dataclass(slots=True)
class FeeSchedule:
    maker: float = 0.0002
    taker: float = 0.0005

    def rate(self, order_type: OrderType) -> float:
        return self.maker if order_type == "maker" else self.taker


@dataclass(slots=True)
class RiskConfig:
    margin_mode: MarginMode = DEFAULT_MARGIN_MODE
    leverage: int = DEFAULT_LEVERAGE
    margin: float = DEFAULT_MARGIN
    take_profit_pct: float = 0.012
    stop_loss_pct: float = 0.006
    trailing_stop_pct: Optional[float] = None
    max_hold_minutes: Optional[int] = None
    maintenance_margin_rate: float = 0.005
    liquidation_fee_rate: float = 0.005
    entry_order_type: OrderType = "taker"
    exit_order_type: OrderType = "taker"

    @classmethod
    def from_hours(cls, *, max_hold_hours: Optional[float] = None, **kwargs: Any) -> "RiskConfig":
        return cls(max_hold_minutes=(int(round(max_hold_hours * 60)) if max_hold_hours is not None else None), **kwargs)

    def validate(self, *, require_max_hold: bool = False) -> None:
        if self.margin_mode != "isolated":
            raise NotImplementedError(
                "cross margin is not implemented; this engine only supports "
                "margin_mode='isolated' until cross has its own shared-pool "
                "accounting model"
            )
        if self.leverage not in ALLOWED_LEVERAGE:
            raise ValueError(f"leverage must be one of {sorted(ALLOWED_LEVERAGE)}")
        if not MIN_MARGIN <= self.margin <= MAX_MARGIN:
            raise ValueError(f"margin must be between ${MIN_MARGIN:.0f} and ${MAX_MARGIN:.0f}")
        if self.take_profit_pct <= 0 or self.stop_loss_pct <= 0:
            raise ValueError("take_profit_pct and stop_loss_pct must be positive")
        if self.trailing_stop_pct is not None and self.trailing_stop_pct <= 0:
            raise ValueError("trailing_stop_pct must be positive")
        if not 0 <= self.maintenance_margin_rate < 1:
            raise ValueError("maintenance_margin_rate must be in [0, 1)")
        if not 0 <= self.liquidation_fee_rate < 1:
            raise ValueError("liquidation_fee_rate must be in [0, 1)")
        if self.max_hold_minutes is not None and not 1 <= self.max_hold_minutes <= 10080:
            raise ValueError("max_hold_minutes must be between 1 and 10080")
        if require_max_hold and self.max_hold_minutes is None:
            raise ValueError("max_hold_minutes is required for timeframe workers")


@dataclass(slots=True)
class Position:
    trade_id: str
    symbol: str
    side: Side
    margin_mode: MarginMode
    leverage: int
    isolated_margin: float
    notional: float
    quantity: float
    entry_price: float
    entry_time: str
    take_profit_price: float
    stop_loss_price: float
    liquidation_price: float
    maintenance_margin_rate: float
    entry_fee: float
    entry_order_type: OrderType
    exit_order_type: OrderType
    trailing_stop_pct: Optional[float] = None
    max_hold_minutes: Optional[int] = None
    high_water_mark: float = 0.0
    low_water_mark: float = 0.0
    accrued_funding: float = 0.0
    signal_reason: str = "manual"
    market_regime: str = "unknown"

    def direction(self) -> int:
        return 1 if self.side == "long" else -1

    def unrealized_pnl(self, mark_price: float) -> float:
        return (mark_price - self.entry_price) * self.quantity * self.direction()

    def margin_roi(self, mark_price: float) -> float:
        return self.unrealized_pnl(mark_price) / self.isolated_margin if self.isolated_margin else 0.0


@dataclass(slots=True)
class ClosedTrade:
    trade_id: str
    symbol: str
    side: Side
    margin_mode: MarginMode
    leverage: int
    margin_used: float
    notional: float
    quantity: float
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    take_profit_price: float
    stop_loss_price: float
    liquidation_price: float
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    funding_paid: float
    liquidation_fee: float
    net_pnl: float
    roi_pct: float
    hold_seconds: float
    entry_reason: str
    exit_reason: str
    market_regime: str
    insurance_fund_shortfall: float = 0.0
    reduce_only: bool = True
    time_in_force: str = "IOC"
    execution_type: str = "taker"


@dataclass(slots=True)
class AccountState:
    schema_version: int
    initial_balance: float
    wallet_balance: float
    reserved_margin: float
    realized_gross_pnl: float
    realized_net_pnl: float
    total_fees: float
    total_funding: float
    total_liquidation_fees: float
    opened_trades: int
    closed_trades: int
    total_insurance_fund_shortfall: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    applied_funding_event_ids: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)

    @property
    def available_balance(self) -> float:
        return self.wallet_balance - self.reserved_margin


class FuturesPaperEngine:
    def __init__(
        self,
        session_dir: str | Path,
        initial_balance: float = 10_000.0,
        fee_schedule: Optional[FeeSchedule] = None,
        acquire_lock: bool = True,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.session_dir / "account.json"
        self.trades_path = self.session_dir / "trades.jsonl"
        self.events_path = self.session_dir / "events.jsonl"
        self.marks_path = self.session_dir / "marks.jsonl"
        self.lock_path = self.session_dir / ".writer.lock"
        self.fee_schedule = fee_schedule or FeeSchedule()
        self._lock = threading.RLock()
        self._own_writer_lock = False

        if acquire_lock:
            self._acquire_writer_lock()

        if self.state_path.exists():
            self.state = self._load_state()
        else:
            if initial_balance <= 0:
                raise ValueError("initial_balance must be positive")
            self.state = AccountState(
                schema_version=1,
                initial_balance=float(initial_balance),
                wallet_balance=float(initial_balance),
                reserved_margin=0.0,
                realized_gross_pnl=0.0,
                realized_net_pnl=0.0,
                total_fees=0.0,
                total_funding=0.0,
                total_liquidation_fees=0.0,
                opened_trades=0,
                closed_trades=0,
            )
            self._validate_state(self.state)
            self._persist_state(self.state)
            append_jsonl(self.events_path, {"timestamp": utc_now(), "event": "account_created", "initial_balance": initial_balance})

    # -- writer lease -----------------------------------------------------

    @staticmethod
    def _process_start_ticks(pid: int) -> int | None:
        """Return Linux process start ticks, which survive PID reuse checks."""
        try:
            # comm may contain spaces and parentheses; field 22 starts after
            # the final ')' and is index 19 in the remaining fields.
            tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            return int(tail[19])
        except (OSError, ValueError, IndexError):
            return None

    def _existing_writer_is_current(self) -> bool:
        try:
            record = json.loads(self.lock_path.read_text(encoding="utf-8"))
            pid = int(record["pid"])
            start_ticks = int(record["start_ticks"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # Legacy PID-only locks cannot distinguish PID 1 across container
            # recreations. They are handled once during migration; every new
            # lock uses the restart-safe structured format below.
            return True
        return self._process_start_ticks(pid) == start_ticks

    def _acquire_writer_lock(self) -> None:
        for attempt in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                if attempt == 0 and not self._existing_writer_is_current():
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                raise WriterLockError(
                    f"session {self.session_dir} already has an active writer "
                    f"(remove {self.lock_path} only if that process is confirmed dead)"
                ) from exc
        else:  # pragma: no cover - loop either opens or raises
            raise WriterLockError(f"could not acquire writer lock for {self.session_dir}")
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({
                "pid": os.getpid(),
                "start_ticks": self._process_start_ticks(os.getpid()),
            }))
        self._own_writer_lock = True
        atexit.register(self.release_writer_lock)

    def release_writer_lock(self) -> None:
        if self._own_writer_lock and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            self._own_writer_lock = False

    def close(self) -> None:
        self.release_writer_lock()

    def __enter__(self) -> "FuturesPaperEngine":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- state (de)serialization -------------------------------------------

    def _serialize_state(self, state: AccountState) -> dict[str, Any]:
        raw = asdict(state)
        raw["positions"] = {trade_id: asdict(position) for trade_id, position in state.positions.items()}
        raw["available_balance"] = state.available_balance
        return raw

    def _load_state(self) -> AccountState:
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        positions = {key: Position(**value) for key, value in raw.get("positions", {}).items()}
        fields = {k: v for k, v in raw.items() if k not in {"positions", "available_balance"}}
        fields.setdefault("applied_funding_event_ids", [])
        return AccountState(positions=positions, **fields)

    def _persist_state(self, state: AccountState) -> None:
        state.updated_at = utc_now()
        atomic_write(self.state_path, json.dumps(self._serialize_state(state), indent=2, ensure_ascii=False))

    def _validate_state(self, state: AccountState) -> None:
        expected_reserved = sum(p.isolated_margin for p in state.positions.values())
        if abs(expected_reserved - state.reserved_margin) > 1e-6:
            raise InvariantViolation(
                f"reserved margin mismatch: state={state.reserved_margin}, positions={expected_reserved}"
            )
        if state.reserved_margin < -EPSILON:
            raise InvariantViolation("reserved margin cannot be negative")
        if state.available_balance < -EPSILON:
            raise InvariantViolation("available balance cannot be negative")

    def _commit(
        self,
        new_state: AccountState,
        event: dict[str, Any],
        trade_row: Optional[dict[str, Any]] = None,
    ) -> None:
        """Validate a proposed state before it ever touches disk or self.state.

        Order: the caller mutates a clone; this validates the clone, persists
        the clone atomically, appends the closed-trade row (if any) and the
        event record, and only then publishes the clone as current state. If
        validation fails, neither disk nor self.state is touched.
        """
        self._validate_state(new_state)
        self._persist_state(new_state)
        if trade_row is not None:
            append_jsonl(self.trades_path, trade_row)
        append_jsonl(self.events_path, event)
        self.state = new_state

    def _liquidation_price(self, entry_price: float, side: Side, leverage: int, maintenance_rate: float) -> float:
        # Approximation for USDT-margined linear perpetuals.
        if side == "long":
            return max(0.0, entry_price * (1.0 - 1.0 / leverage + maintenance_rate))
        return entry_price * (1.0 + 1.0 / leverage - maintenance_rate)

    def _validate_open_request(self, symbol: str, side: Side, price: float, risk: RiskConfig) -> None:
        risk.validate()
        if not symbol:
            raise ValueError("symbol is required")
        if side not in ("long", "short"):
            raise ValueError("side must be long or short")
        if price <= 0 or not math.isfinite(price):
            raise ValueError("price must be a finite positive number")

    def open_position(
        self,
        symbol: str,
        side: Side,
        *,
        price: Optional[float] = None,
        risk: Optional[RiskConfig] = None,
        signal_reason: str = "manual",
        market_regime: str = "unknown",
    ) -> Position:
        with self._lock:
            cfg = risk or RiskConfig()
            symbol = normalize_symbol(symbol)
            entry_price = float(price if price is not None else fetch_mark_price(symbol))
            self._validate_open_request(symbol, side, entry_price, cfg)

            notional = cfg.margin * cfg.leverage
            entry_fee = notional * self.fee_schedule.rate(cfg.entry_order_type)
            required_cash = cfg.margin + entry_fee
            if self.state.available_balance + EPSILON < required_cash:
                raise RuntimeError(
                    f"insufficient available balance: need ${required_cash:.2f}, "
                    f"have ${self.state.available_balance:.2f}"
                )

            direction = 1 if side == "long" else -1
            quantity = notional / entry_price
            tp = entry_price * (1.0 + direction * cfg.take_profit_pct)
            sl = entry_price * (1.0 - direction * cfg.stop_loss_pct)
            liquidation = self._liquidation_price(
                entry_price, side, cfg.leverage, cfg.maintenance_margin_rate
            )
            if side == "long" and not liquidation < sl < entry_price < tp:
                raise ValueError("invalid long TP/SL/liquidation ordering")
            if side == "short" and not tp < entry_price < sl < liquidation:
                raise ValueError("invalid short TP/SL/liquidation ordering")

            trade_id = uuid.uuid4().hex[:16]
            position = Position(
                trade_id=trade_id,
                symbol=symbol,
                side=side,
                margin_mode=cfg.margin_mode,
                leverage=cfg.leverage,
                isolated_margin=cfg.margin,
                notional=notional,
                quantity=quantity,
                entry_price=entry_price,
                entry_time=utc_now(),
                take_profit_price=tp,
                stop_loss_price=sl,
                liquidation_price=liquidation,
                maintenance_margin_rate=cfg.maintenance_margin_rate,
                entry_fee=entry_fee,
                entry_order_type=cfg.entry_order_type,
                exit_order_type=cfg.exit_order_type,
                trailing_stop_pct=cfg.trailing_stop_pct,
                max_hold_minutes=cfg.max_hold_minutes,
                high_water_mark=entry_price,
                low_water_mark=entry_price,
                signal_reason=signal_reason,
                market_regime=market_regime,
            )

            new_state = copy.deepcopy(self.state)
            new_state.wallet_balance -= entry_fee
            new_state.reserved_margin += cfg.margin
            new_state.total_fees += entry_fee
            new_state.opened_trades += 1
            new_state.positions[trade_id] = position

            event = {
                "timestamp": utc_now(),
                "event": "position_opened",
                **asdict(position),
                "expected_tp_gross": notional * cfg.take_profit_pct,
                "expected_sl_gross": -(notional * cfg.stop_loss_pct),
                "expected_tp_net": notional * cfg.take_profit_pct - entry_fee - notional * self.fee_schedule.rate(cfg.exit_order_type),
                "expected_sl_net": -(notional * cfg.stop_loss_pct) - entry_fee - notional * self.fee_schedule.rate(cfg.exit_order_type),
            }
            self._commit(new_state, event)
            return position

    def apply_funding(self, trade_id: str, funding_rate: float, *, event_id: Optional[str] = None) -> float:
        """Apply a funding payment. `event_id` (e.g. 'BTCUSDT:2026-07-25T08:00:00Z')
        makes this idempotent across restarts: replays of the same window are
        a no-op instead of double-charging the wallet.
        """
        with self._lock:
            position = self.state.positions.get(trade_id)
            if position is None:
                raise KeyError(f"unknown open trade_id: {trade_id}")

            if event_id is not None and event_id in self.state.applied_funding_event_ids:
                append_jsonl(self.events_path, {
                    "timestamp": utc_now(),
                    "event": "funding_skipped_duplicate",
                    "trade_id": trade_id,
                    "symbol": position.symbol,
                    "event_id": event_id,
                })
                return 0.0

            payment = position.notional * float(funding_rate) * position.direction()
            # Positive funding: long pays (+), short receives (-).

            new_state = copy.deepcopy(self.state)
            new_position = new_state.positions[trade_id]
            new_position.accrued_funding += payment
            new_state.wallet_balance -= payment
            new_state.total_funding += payment
            if event_id is not None:
                new_state.applied_funding_event_ids.append(event_id)

            event = {
                "timestamp": utc_now(),
                "event": "funding_applied",
                "trade_id": trade_id,
                "symbol": position.symbol,
                "funding_rate": funding_rate,
                "payment": payment,
                "event_id": event_id,
            }
            self._commit(new_state, event)
            return payment

    def close_position(
        self,
        trade_id: str,
        *,
        price: Optional[float] = None,
        exit_reason: str = "manual",
        order_type: Optional[OrderType] = None,
        liquidation_fee_rate: float = 0.005,
    ) -> ClosedTrade:
        with self._lock:
            position = self.state.positions.get(trade_id)
            if position is None:
                raise KeyError(f"unknown open trade_id: {trade_id}")

            exit_price = float(price if price is not None else fetch_mark_price(position.symbol))
            if exit_price <= 0 or not math.isfinite(exit_price):
                raise ValueError("exit price must be finite and positive")

            exit_type = order_type or position.exit_order_type
            gross = position.unrealized_pnl(exit_price)
            exit_notional = abs(position.quantity * exit_price)
            exit_fee = exit_notional * self.fee_schedule.rate(exit_type)
            liquidation_fee = (
                exit_notional * liquidation_fee_rate if exit_reason == "liquidation" else 0.0
            )
            uncapped_net = gross - position.entry_fee - exit_fee - position.accrued_funding - liquidation_fee
            insurance_fund_shortfall = 0.0
            net = uncapped_net
            if exit_reason == "liquidation":
                net = max(-position.isolated_margin, uncapped_net)
                insurance_fund_shortfall = max(0.0, -position.isolated_margin - uncapped_net)
            exit_time = utc_now()
            hold_seconds = (
                datetime.fromisoformat(exit_time) - datetime.fromisoformat(position.entry_time)
            ).total_seconds()

            closed = ClosedTrade(
                trade_id=position.trade_id,
                symbol=position.symbol,
                side=position.side,
                margin_mode=position.margin_mode,
                leverage=position.leverage,
                margin_used=position.isolated_margin,
                notional=position.notional,
                quantity=position.quantity,
                entry_time=position.entry_time,
                exit_time=exit_time,
                entry_price=position.entry_price,
                exit_price=exit_price,
                take_profit_price=position.take_profit_price,
                stop_loss_price=position.stop_loss_price,
                liquidation_price=position.liquidation_price,
                gross_pnl=gross,
                entry_fee=position.entry_fee,
                exit_fee=exit_fee,
                funding_paid=position.accrued_funding,
                liquidation_fee=liquidation_fee,
                net_pnl=net,
                roi_pct=(net / position.isolated_margin) * 100.0,
                hold_seconds=hold_seconds,
                entry_reason=position.signal_reason,
                exit_reason=exit_reason,
                market_regime=position.market_regime,
                insurance_fund_shortfall=insurance_fund_shortfall,
                reduce_only=True,
                time_in_force="IOC",
                execution_type=exit_type,
            )

            new_state = copy.deepcopy(self.state)
            new_state.reserved_margin -= position.isolated_margin
            # Existing non-liquidation economics are unchanged.  A liquidation
            # settles no worse than this position's isolated collateral.
            new_state.wallet_balance += (gross - exit_fee - liquidation_fee) if exit_reason != "liquidation" else (net + position.entry_fee + position.accrued_funding)
            new_state.total_fees += exit_fee
            new_state.total_liquidation_fees += liquidation_fee
            new_state.total_insurance_fund_shortfall += insurance_fund_shortfall
            new_state.realized_gross_pnl += gross
            new_state.realized_net_pnl += net
            new_state.closed_trades += 1
            del new_state.positions[trade_id]

            event = {"timestamp": utc_now(), "event": "position_closed", **asdict(closed)}
            self._commit(new_state, event, trade_row=asdict(closed))
            return closed

    def snapshot(self, prices: Optional[dict[str, float]] = None) -> dict[str, Any]:
        """Pure read: computes the current account view without writing anything
        to disk. Safe to call from dashboards/MCP reads on every refresh.
        """
        with self._lock:
            supplied = {normalize_symbol(k): float(v) for k, v in (prices or {}).items()}
            position_rows: list[dict[str, Any]] = []
            total_unrealized = 0.0

            for position in self.state.positions.values():
                mark_price = supplied.get(position.symbol)
                if mark_price is None:
                    mark_price = fetch_mark_price(position.symbol)
                total_unrealized += position.unrealized_pnl(mark_price)
                position_rows.append(self.position_snapshot(position, mark_price))

            equity = self.state.wallet_balance + total_unrealized
            return {
                "timestamp": utc_now(),
                "initial_balance": self.state.initial_balance,
                "wallet_balance": self.state.wallet_balance,
                "available_balance": self.state.available_balance,
                "reserved_margin": self.state.reserved_margin,
                "open_notional": sum(p.notional for p in self.state.positions.values()),
                "realized_gross_pnl": self.state.realized_gross_pnl,
                "realized_net_pnl": self.state.realized_net_pnl,
                "unrealized_pnl": total_unrealized,
                "fees_paid": self.state.total_fees,
                "funding_paid": self.state.total_funding,
                "liquidation_fees": self.state.total_liquidation_fees,
                "current_equity": equity,
                "pnl": equity - self.state.initial_balance,
                "pnl_pct": ((equity / self.state.initial_balance) - 1.0) * 100.0,
                "open_positions": position_rows,
            }

    def record_mark(
        self,
        prices: Optional[dict[str, float]] = None,
        market_data_source: str | None = None,
        market_data_observed_at: str | None = None,
    ) -> dict[str, Any]:
        """Impure: takes a snapshot and appends it to marks.jsonl.

        Call this explicitly from the poll/tick loop, never from a passive
        read path.
        """
        with self._lock:
            snap = self.snapshot(prices)
            if market_data_source is not None:
                snap["market_data_source"] = market_data_source
            if market_data_observed_at is not None:
                snap["market_data_observed_at"] = market_data_observed_at
            append_jsonl(self.marks_path, snap)
            return snap

    def position_snapshot(self, position: Position, mark_price: float) -> dict[str, Any]:
        gross = position.unrealized_pnl(mark_price)
        estimated_exit_fee = abs(position.quantity * mark_price) * self.fee_schedule.rate(position.exit_order_type)
        return {
            **asdict(position),
            "mark_price": mark_price,
            "unrealized_gross_pnl": gross,
            "estimated_exit_fee": estimated_exit_fee,
            "unrealized_net_pnl": gross - position.entry_fee - estimated_exit_fee - position.accrued_funding,
            "margin_roi_pct": position.margin_roi(mark_price) * 100.0,
            "tp_price_distance_pct": abs(position.take_profit_price / position.entry_price - 1.0) * 100.0,
            "sl_price_distance_pct": abs(position.stop_loss_price / position.entry_price - 1.0) * 100.0,
            "tp_margin_roi_pct_gross": abs(position.take_profit_price / position.entry_price - 1.0) * position.leverage * 100.0,
            "sl_margin_roi_pct_gross": -abs(position.stop_loss_price / position.entry_price - 1.0) * position.leverage * 100.0,
        }

    def process_price(self, trade_id: str, mark_price: float) -> Optional[ClosedTrade]:
        with self._lock:
            position = self.state.positions.get(trade_id)
            if position is None:
                return None

            position.high_water_mark = max(position.high_water_mark, mark_price)
            position.low_water_mark = min(position.low_water_mark, mark_price)

            reason: Optional[str] = None
            if position.side == "long":
                if mark_price <= position.liquidation_price:
                    reason = "liquidation"
                elif mark_price <= position.stop_loss_price:
                    reason = "stop_loss"
                elif mark_price >= position.take_profit_price:
                    reason = "take_profit"
                elif position.trailing_stop_pct is not None:
                    trail = position.high_water_mark * (1.0 - position.trailing_stop_pct)
                    if position.high_water_mark > position.entry_price and mark_price <= trail:
                        reason = "trailing_stop"
            else:
                if mark_price >= position.liquidation_price:
                    reason = "liquidation"
                elif mark_price >= position.stop_loss_price:
                    reason = "stop_loss"
                elif mark_price <= position.take_profit_price:
                    reason = "take_profit"
                elif position.trailing_stop_pct is not None:
                    trail = position.low_water_mark * (1.0 + position.trailing_stop_pct)
                    if position.low_water_mark < position.entry_price and mark_price >= trail:
                        reason = "trailing_stop"

            if reason is None and position.max_hold_minutes is not None:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(position.entry_time)
                if age.total_seconds() >= position.max_hold_minutes * 60:
                    reason = "max_hold"

            if reason is not None:
                return self.close_position(trade_id, price=mark_price, exit_reason=reason, order_type="taker")
            return None

    def process_all(
        self,
        prices: Optional[dict[str, float]] = None,
        market_data_source: str | None = None,
        market_data_observed_at: str | None = None,
    ) -> list[ClosedTrade]:
        supplied = {normalize_symbol(k): float(v) for k, v in (prices or {}).items()}
        closed: list[ClosedTrade] = []
        for trade_id, position in list(self.state.positions.items()):
            mark_price = supplied.get(position.symbol)
            if mark_price is None:
                mark_price = fetch_mark_price(position.symbol)
            result = self.process_price(trade_id, mark_price)
            if result is not None:
                closed.append(result)
        self.record_mark(supplied, market_data_source=market_data_source, market_data_observed_at=market_data_observed_at)
        return closed

    def account_summary(self, prices: Optional[dict[str, float]] = None) -> dict[str, Any]:
        """Pure read path for dashboards/MCP. Does not write marks.jsonl."""
        return self.snapshot(prices)

    def closed_trades(self) -> list[dict[str, Any]]:
        return read_jsonl(self.trades_path)

    def cycle_report(self, since_iso: Optional[str] = None, until_iso: Optional[str] = None) -> dict[str, Any]:
        rows = self.closed_trades()
        since = datetime.fromisoformat(since_iso) if since_iso else None
        until = datetime.fromisoformat(until_iso) if until_iso else None
        filtered: list[dict[str, Any]] = []
        for row in rows:
            dt = datetime.fromisoformat(row["exit_time"])
            if since and dt < since:
                continue
            if until and dt > until:
                continue
            filtered.append(row)

        wins = [r for r in filtered if r["net_pnl"] > 0]
        losses = [r for r in filtered if r["net_pnl"] < 0]
        gross_profit = sum(r["net_pnl"] for r in wins)
        gross_loss = -sum(r["net_pnl"] for r in losses)
        net = sum(r["net_pnl"] for r in filtered)
        fees = sum(r["entry_fee"] + r["exit_fee"] for r in filtered)
        funding = sum(r["funding_paid"] for r in filtered)
        return {
            "since": since_iso,
            "until": until_iso,
            "trades_closed": len(filtered),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": (len(wins) / len(filtered) * 100.0) if filtered else None,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "fees": fees,
            "funding": funding,
            "net_realized_pnl": net,
            "best_trade": max((r["net_pnl"] for r in filtered), default=None),
            "worst_trade": min((r["net_pnl"] for r in filtered), default=None),
            "trades": filtered,
        }


def funding_event_id(symbol: str, funding_window: str) -> str:
    return f"{normalize_symbol(symbol)}:{funding_window}"


def run_poll_loop(
    engine: FuturesPaperEngine,
    poll_seconds: int = 5,
    apply_funding: bool = False,
) -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            if apply_funding and now.hour in (0, 8, 16):
                funding_window = now.strftime("%Y-%m-%dT%H:00:00Z")
                for trade_id, position in list(engine.state.positions.items()):
                    rate = fetch_funding_rate(position.symbol)
                    event_id = funding_event_id(position.symbol, funding_window)
                    engine.apply_funding(trade_id, rate, event_id=event_id)

            closed = engine.process_all()
            snapshot = engine.account_summary()
            print(json.dumps({
                "event": "tick",
                "timestamp": snapshot["timestamp"],
                "equity": snapshot["current_equity"],
                "open_positions": len(snapshot["open_positions"]),
                "closed_this_tick": [asdict(t) for t in closed],
            }), flush=True)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(json.dumps({"event": "poll_error", "error": str(exc), "timestamp": utc_now()}), flush=True)
        time.sleep(max(1, poll_seconds))
