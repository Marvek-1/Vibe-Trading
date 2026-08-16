#!/usr/bin/env python3
"""Adapter between a signal source and FuturesPaperEngine.

The engine (futures_paper_engine.py) owns accounting: opening, marking,
funding, exits, liquidation. This module's only job is to turn a signal into
an explicit long/short/close intent and hand it to the engine's public API --
it never writes to account.json/trades.jsonl/events.jsonl directly.

There is no reusable "many_bots signal function" to call: the existing
many_bots_10x session ran periodic_equal_weight_rebalance, a spot rebalancing
strategy, not a discrete long/short signal generator, and it is the very
strategy already retired for producing an invalid leverage ledger. The
`FrozenMomentumSignal` below is a small, explicit, deterministic placeholder
so the engine + adapter path can be exercised end-to-end; it is a
deployment-safety vehicle, not a claim of profitability. Swap it out once a
real signal has cleared the strategy promotion gates.

Guarantees enforced here:
- frozen symbol universe, loaded from a hash-stamped JSON file;
- one open position per (account, symbol);
- deterministic intent IDs so a PM2 restart cannot double-open;
- an adapter-owned processed-intent ledger, separate from engine files;
- stop (raise) on missing/stale price snapshot instead of guessing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from futures_paper_engine import FuturesPaperEngine, RiskConfig, normalize_symbol

Action = Literal["OPEN_LONG", "OPEN_SHORT", "CLOSE"]

STRATEGY_VERSION = "frozen_momentum_v1"


@dataclass(frozen=True)
class FuturesIntent:
    intent_id: str
    timestamp: str
    strategy_id: str
    symbol: str
    action: Action
    reason: str
    signal_version: str
    config_hash: str


class StalePriceError(RuntimeError):
    pass


class DuplicateIntentError(RuntimeError):
    pass


def load_frozen_universe(path: str | Path) -> list[str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    symbols = payload["symbols"]
    expected_hash = payload["symbols_sha256"]
    actual_hash = hashlib.sha256(
        json.dumps(symbols, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(
            f"universe file {path} is corrupted or was hand-edited: "
            f"hash mismatch (expected {expected_hash}, got {actual_hash})"
        )
    return list(symbols)


def config_hash(*, leverage: int, margin: float, universe_hash: str) -> str:
    payload = json.dumps(
        {"leverage": leverage, "margin": margin, "universe_hash": universe_hash, "strategy_version": STRATEGY_VERSION},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_intent_id(
    *,
    strategy_version: str,
    account_id: str,
    symbol: str,
    action: Action,
    signal_timestamp: str,
    signal_payload_hash: str,
) -> str:
    payload = "|".join([strategy_version, account_id, symbol, action, signal_timestamp, signal_payload_hash])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class FrozenMomentumSignal:
    """Deterministic placeholder signal: compares the current price snapshot
    against the previous one for the same symbol and calls a direction only
    past a fixed threshold. No parameters are tuned, no runtime thresholds
    are changeable, and it holds no state beyond the last observed price per
    symbol (kept in-memory by the caller across ticks).
    """

    threshold_pct = 0.01

    def evaluate(self, symbol: str, prev_price: Optional[float], current_price: float, timestamp: str) -> Optional[Action]:
        if prev_price is None or prev_price <= 0:
            return None
        change = (current_price - prev_price) / prev_price
        if change >= self.threshold_pct:
            return "OPEN_LONG"
        if change <= -self.threshold_pct:
            return "OPEN_SHORT"
        return None


class ManyBotsFuturesAdapter:
    def __init__(
        self,
        engine: FuturesPaperEngine,
        *,
        account_id: str,
        universe_path: str | Path,
        leverage: int,
        margin: float = 20.0,
        ledger_path: Optional[str | Path] = None,
        signal: Optional[FrozenMomentumSignal] = None,
    ) -> None:
        self.engine = engine
        self.account_id = account_id
        self.leverage = leverage
        self.margin = margin
        self.signal = signal or FrozenMomentumSignal()

        universe_payload = json.loads(Path(universe_path).read_text(encoding="utf-8"))
        self.universe = load_frozen_universe(universe_path)
        self._normalized_universe = {normalize_symbol(s) for s in self.universe}
        self.universe_hash = universe_payload["symbols_sha256"]
        self._config_hash = config_hash(leverage=leverage, margin=margin, universe_hash=self.universe_hash)

        self.ledger_path = Path(ledger_path) if ledger_path else engine.session_dir / "adapter_processed_intents.json"
        self._processed_intent_ids: set[str] = set(self._load_ledger())
        self._last_price: dict[str, float] = {}
        self.last_diagnostics: dict[str, object] = {}

    def _load_ledger(self) -> list[str]:
        if not self.ledger_path.exists():
            return []
        return json.loads(self.ledger_path.read_text(encoding="utf-8")).get("processed_intent_ids", [])

    def _persist_ledger(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"processed_intent_ids": sorted(self._processed_intent_ids)}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.ledger_path)

    def _open_symbol_or_none(self, symbol: str) -> Optional[str]:
        symbol = normalize_symbol(symbol)
        for trade_id, position in self.engine.state.positions.items():
            if position.symbol == symbol:
                return trade_id
        return None

    def build_intent(self, symbol: str, action: Action, *, timestamp: str, reason: str) -> FuturesIntent:
        symbol = normalize_symbol(symbol)
        signal_payload_hash = hashlib.sha256(f"{symbol}:{action}:{timestamp}".encode()).hexdigest()[:16]
        intent_id = make_intent_id(
            strategy_version=STRATEGY_VERSION,
            account_id=self.account_id,
            symbol=symbol,
            action=action,
            signal_timestamp=timestamp,
            signal_payload_hash=signal_payload_hash,
        )
        return FuturesIntent(
            intent_id=intent_id,
            timestamp=timestamp,
            strategy_id=STRATEGY_VERSION,
            symbol=symbol,
            action=action,
            reason=reason,
            signal_version=STRATEGY_VERSION,
            config_hash=self._config_hash,
        )

    def apply_intent(self, intent: FuturesIntent, *, price: Optional[float] = None) -> Optional[str]:
        """Executes an intent against the engine. Returns the trade_id opened,
        or None if the intent was a no-op (duplicate, symbol not frozen,
        or a position already open on that symbol for an OPEN_* action).
        Raises on anything that indicates drift rather than silently
        continuing.
        """
        if intent.config_hash != self._config_hash:
            raise RuntimeError(
                f"intent config_hash {intent.config_hash} does not match this "
                f"adapter's config_hash {self._config_hash} -- refusing to "
                f"apply an intent built under a different leverage/margin/universe"
            )
        if intent.symbol not in self._normalized_universe:
            raise RuntimeError(f"symbol {intent.symbol} is not in the frozen universe; refusing intent")

        if intent.intent_id in self._processed_intent_ids:
            return None

        existing_trade_id = self._open_symbol_or_none(intent.symbol)

        if intent.action == "CLOSE":
            if existing_trade_id is None:
                self._mark_processed(intent)
                return None
            self.engine.close_position(existing_trade_id, exit_reason=intent.reason)
            self._mark_processed(intent)
            return existing_trade_id

        # OPEN_LONG / OPEN_SHORT: enforce one open position per symbol.
        if existing_trade_id is not None:
            self._mark_processed(intent)
            return None

        side = "long" if intent.action == "OPEN_LONG" else "short"
        risk = RiskConfig(margin_mode="isolated", margin=self.margin, leverage=self.leverage)
        position = self.engine.open_position(
            intent.symbol,
            side,
            price=price,
            risk=risk,
            signal_reason=intent.reason,
        )
        self._mark_processed(intent)
        return position.trade_id

    def _mark_processed(self, intent: FuturesIntent) -> None:
        self._processed_intent_ids.add(intent.intent_id)
        self._persist_ledger()

    def evaluate_price_tick(self, prices: dict[str, float], *, timestamp: Optional[str] = None) -> list[FuturesIntent]:
        """Evaluate one complete price snapshot without placing paper orders.

        This is deliberately separate from :meth:`apply_intents`: paper mode
        can report real signal activity while the global new-entry gate is
        closed.  That makes a disabled execution policy distinguishable from a
        strategy that found no candidate.
        """
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        normalized_prices = {normalize_symbol(k): float(v) for k, v in prices.items()}
        missing = [s for s in self.universe if normalize_symbol(s) not in normalized_prices]
        if missing:
            self.last_diagnostics = {
                "symbols_available": len(normalized_prices),
                "symbols_scanned": 0,
                "bars_received": len(normalized_prices),
                "bars_rejected_stale": len(missing),
                "indicators_computed": 0,
                "signals_evaluated": 0,
                "signals_true": 0,
                "signals_false": 0,
                "rejection_reasons": {"market_snapshot_incomplete": len(missing)},
            }
            raise StalePriceError(f"price snapshot missing frozen-universe symbols: {missing}")

        intents: list[FuturesIntent] = []
        for symbol in self.universe:
            key = normalize_symbol(symbol)
            current_price = normalized_prices[key]
            action = self.signal.evaluate(key, self._last_price.get(key), current_price, ts)
            self._last_price[key] = current_price
            if action is None:
                continue
            intent = self.build_intent(symbol, action, timestamp=ts, reason=f"frozen_momentum:{action}")
            intents.append(intent)

        self.last_diagnostics = {
            "symbols_available": len(normalized_prices),
            "symbols_scanned": len(self.universe),
            "bars_received": len(self.universe),
            "bars_rejected_stale": 0,
            "indicators_computed": len(self.universe),
            "signals_evaluated": len(self.universe),
            "signals_true": len(intents),
            "signals_false": len(self.universe) - len(intents),
            "entries_requested": 0,
            "entries_rejected_risk": 0,
            "entries_rejected_cooldown": 0,
            "entries_rejected_position_limit": 0,
            "entries_rejected_session": 0,
            "entries_rejected_duplicate": 0,
            "entries_rejected_other": 0,
            "paper_orders_submitted": 0,
            "paper_orders_filled": 0,
            "rejection_reasons": {},
        }
        return intents

    def apply_intents(self, intents: list[FuturesIntent], prices: dict[str, float]) -> list[FuturesIntent]:
        """Apply already-evaluated intents and retain their exact outcomes."""
        applied: list[FuturesIntent] = []
        diagnostics = self.last_diagnostics
        reasons = dict(diagnostics.get("rejection_reasons", {}))
        normalized_prices = {normalize_symbol(key): float(value) for key, value in prices.items()}
        for intent in intents:
            diagnostics["entries_requested"] = int(diagnostics.get("entries_requested", 0)) + 1
            if intent.intent_id in self._processed_intent_ids:
                diagnostics["entries_rejected_duplicate"] = int(diagnostics.get("entries_rejected_duplicate", 0)) + 1
                reasons["duplicate_intent"] = int(reasons.get("duplicate_intent", 0)) + 1
                continue
            if intent.action != "CLOSE" and self._open_symbol_or_none(intent.symbol) is not None:
                diagnostics["entries_rejected_position_limit"] = int(diagnostics.get("entries_rejected_position_limit", 0)) + 1
                reasons["position_already_open"] = int(reasons.get("position_already_open", 0)) + 1
                continue
            trade_id = self.apply_intent(intent, price=normalized_prices[normalize_symbol(intent.symbol)])
            if trade_id is not None:
                applied.append(intent)
                diagnostics["paper_orders_submitted"] = int(diagnostics.get("paper_orders_submitted", 0)) + 1
                diagnostics["paper_orders_filled"] = int(diagnostics.get("paper_orders_filled", 0)) + 1
            else:
                diagnostics["entries_rejected_other"] = int(diagnostics.get("entries_rejected_other", 0)) + 1
                reasons["intent_noop"] = int(reasons.get("intent_noop", 0)) + 1
        diagnostics["rejection_reasons"] = reasons
        return applied

    def on_price_tick(self, prices: dict[str, float], *, timestamp: Optional[str] = None) -> list[FuturesIntent]:
        """Evaluate and apply a complete price snapshot."""
        return self.apply_intents(self.evaluate_price_tick(prices, timestamp=timestamp), prices)
