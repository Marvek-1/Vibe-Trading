"""bounded_grid_v1: a fixed-size, capped-exposure grid strategy.

Pure decision logic only -- this module never touches account.json, never
calls a broker, and never imports FuturesPaperEngine. The caller (see
scripts/bounded_grid_runner.py) is responsible for turning the Intents this
produces into engine.open_position() calls and for calling
engine.process_all() every tick so the engine's own TP/SL/liquidation path
handles every exit. This module only ever proposes entries.

Design constraints (all enforced here, not left to the runner):
- fixed margin per level -- no martingale, no widening size on losers.
- a hard cap on simultaneously open levels (max_open_levels), counted across
  both the long ladder (below center) and the short ladder (above center),
  not per side -- so total exposure is bounded regardless of direction mix.
- a hard cap on total notional across all open grid positions.
- the grid center is only ever set from a real mark price, and only while
  flat (zero open grid positions) -- so the ladder can never be dragged
  around underneath a position that's already live.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, Optional

Action = Literal["OPEN_LONG", "OPEN_SHORT"]

STRATEGY_VERSION = "bounded_grid_v1"


@dataclass(frozen=True, slots=True)
class BoundedGridConfig:
    symbol: str
    leverage: int
    margin_per_level: float
    max_total_notional: float
    levels_per_side: int = 3
    max_open_levels: int = 3
    grid_spacing_bps: float = 25.0
    take_profit_bps: float = 30.0
    stop_loss_bps: float = 90.0

    def validate(self) -> None:
        if self.leverage not in (5, 10):
            raise ValueError("leverage must be 5 or 10")
        if self.margin_per_level <= 0:
            raise ValueError("margin_per_level must be positive")
        if self.max_total_notional <= 0:
            raise ValueError("max_total_notional must be positive")
        if self.levels_per_side < 1:
            raise ValueError("levels_per_side must be >= 1")
        if self.max_open_levels < 1:
            raise ValueError("max_open_levels must be >= 1")
        if self.grid_spacing_bps <= 0 or self.take_profit_bps <= 0 or self.stop_loss_bps <= 0:
            raise ValueError("spacing/take_profit/stop_loss bps must be positive")
        level_notional = self.margin_per_level * self.leverage
        if level_notional * self.max_open_levels > self.max_total_notional + 1e-9:
            raise ValueError(
                f"max_open_levels ({self.max_open_levels}) at margin_per_level "
                f"({self.margin_per_level}) x {self.leverage}x exceeds "
                f"max_total_notional ({self.max_total_notional})"
            )


def config_hash(config: BoundedGridConfig) -> str:
    payload = json.dumps(
        {"strategy_version": STRATEGY_VERSION, "config": asdict(config)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class GridLevel:
    """One rung of the ladder: `index` counts outward from center (1..levels_per_side),
    `side` says which ladder it belongs to. `trigger_price` is the price at
    which this level opens a position -- long levels trigger on price
    dropping to/through it, short levels trigger on price rising to/through it.
    """
    side: Literal["long", "short"]
    index: int
    trigger_price: float


@dataclass(frozen=True, slots=True)
class GridIntent:
    action: Action
    level_id: str
    trigger_price: float
    margin: float
    reason: str


class BoundedGridStrategy:
    """Stateful grid ladder for a single symbol/account.

    State (center price + which levels are occupied by which trade_id) is
    exposed via export_state()/restore_state() so the runner can persist it
    across restarts the same way many_bots_futures_adapter.py does for its
    signal state -- a restart must not forget which levels are occupied and
    re-fire them, since that would be exactly the "unlimited averaging"
    behavior this strategy is designed to prevent.
    """

    version = STRATEGY_VERSION

    def __init__(self, config: BoundedGridConfig) -> None:
        config.validate()
        self.config = config
        self.center_price: Optional[float] = None
        # level_id ("long:1", "short:2", ...) -> trade_id currently occupying it
        self.occupied: dict[str, str] = {}
        # True once >=1 level has been filled since the last re-center. Used
        # to tell "never traded yet, center stays put" apart from "just
        # finished a full cycle, re-center now" -- both look like "occupied
        # is empty" but only the second should move the center. Without this,
        # the very first idle tick after center is set would immediately
        # re-center to the live price too, so the ladder would permanently
        # chase price and no level could ever be crossed.
        self._traded_since_center: bool = False

    def _levels(self) -> list[GridLevel]:
        if self.center_price is None:
            return []
        spacing = self.config.grid_spacing_bps / 10_000.0
        levels: list[GridLevel] = []
        for i in range(1, self.config.levels_per_side + 1):
            levels.append(GridLevel("long", i, self.center_price * (1.0 - spacing * i)))
            levels.append(GridLevel("short", i, self.center_price * (1.0 + spacing * i)))
        return levels

    @staticmethod
    def _level_id(side: str, index: int) -> str:
        return f"{side}:{index}"

    def reconcile_open_trade_ids(self, open_trade_ids: set[str]) -> None:
        """Free any level whose position the engine no longer shows as open
        (closed via TP/SL/liquidation since the last tick)."""
        for level_id, trade_id in list(self.occupied.items()):
            if trade_id not in open_trade_ids:
                del self.occupied[level_id]

    def on_price_tick(self, mark_price: float, open_trade_ids: set[str]) -> list[GridIntent]:
        if mark_price <= 0:
            raise ValueError(f"invalid mark price {mark_price!r}")
        self.reconcile_open_trade_ids(open_trade_ids)

        if self.center_price is None:
            self.center_price = mark_price
        elif not self.occupied and self._traded_since_center:
            # Flat again after a full cycle -- re-anchor to the live market
            # instead of trading further and further from a stale center.
            self.center_price = mark_price
            self._traded_since_center = False

        if len(self.occupied) >= self.config.max_open_levels:
            return []

        level_notional = self.config.margin_per_level * self.config.leverage
        current_notional = len(self.occupied) * level_notional
        intents: list[GridIntent] = []

        for level in self._levels():
            if len(self.occupied) + len(intents) >= self.config.max_open_levels:
                break
            level_id = self._level_id(level.side, level.index)
            if level_id in self.occupied:
                continue
            if current_notional + level_notional * (len(intents) + 1) > self.config.max_total_notional + 1e-9:
                continue
            triggered = (
                (level.side == "long" and mark_price <= level.trigger_price)
                or (level.side == "short" and mark_price >= level.trigger_price)
            )
            if not triggered:
                continue
            intents.append(GridIntent(
                action="OPEN_LONG" if level.side == "long" else "OPEN_SHORT",
                level_id=level_id,
                trigger_price=level.trigger_price,
                margin=self.config.margin_per_level,
                reason=f"bounded_grid:{level_id}@{level.trigger_price:.6f}",
            ))
        return intents

    def mark_level_filled(self, level_id: str, trade_id: str) -> None:
        self.occupied[level_id] = trade_id
        self._traded_since_center = True

    def export_state(self) -> dict[str, object]:
        return {
            "strategy_version": self.version,
            "config": asdict(self.config),
            "center_price": self.center_price,
            "occupied": dict(self.occupied),
            "traded_since_center": self._traded_since_center,
        }

    def restore_state(self, payload: dict[str, object]) -> None:
        if payload.get("strategy_version") != self.version:
            raise ValueError("grid-state strategy version does not match")
        if payload.get("config") != asdict(self.config):
            raise ValueError("grid-state configuration does not match")
        center = payload.get("center_price")
        if center is not None:
            center = float(center)
            if center <= 0:
                raise ValueError("invalid persisted center_price")
        self.center_price = center
        occupied = payload.get("occupied", {})
        if not isinstance(occupied, dict):
            raise ValueError("invalid grid-state occupied levels")
        self.occupied = {str(k): str(v) for k, v in occupied.items()}
        self._traded_since_center = bool(payload.get("traded_since_center", bool(self.occupied)))
