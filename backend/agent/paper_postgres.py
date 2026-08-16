"""Account-scoped PostgreSQL persistence for futures paper workers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from uuid import uuid4

import psycopg

from positions_reconciliation import reconcile_positions, validate_position_snapshot

ALLOWED_LEVERAGE = {5, 10}


@dataclass(frozen=True)
class WorkerIdentity:
    account_id: UUID
    strategy_id: str
    worker_id: str
    timeframe: str
    mode: str
    leverage: int

    def validate(self) -> None:
        if not self.strategy_id or not self.worker_id:
            raise ValueError("strategy_id and worker_id are required")
        if self.timeframe not in {"5m", "10m", "15m", "tick"}:
            raise ValueError("timeframe must be 5m, 10m, 15m, or tick")
        if self.mode != "paper":
            if os.getenv("ENABLE_LIVE_TRADING", "false").lower() != "true":
                raise ValueError("live trading is locked; ENABLE_LIVE_TRADING is not true")
        if self.leverage not in ALLOWED_LEVERAGE:
            raise ValueError("leverage must be 5x or 10x")


class PaperPostgres:
    def __init__(self, identity: WorkerIdentity, dsn: str | None = None) -> None:
        identity.validate()
        self.identity = identity
        self.dsn = dsn or os.getenv("VIBE_PAPER_DATABASE_URL", "dbname=idim_ikang port=5433")
        self._verify_account()
        self._lease = self._acquire_lease()

    def _acquire_lease(self):
        lease = self._connect()
        # This connection is held for the worker's lifetime solely to own the
        # session-level advisory lock.  Keep its probes out of a transaction;
        # otherwise every ``SELECT 1`` in _ensure_lease() remains ``idle in
        # transaction`` and pins PostgreSQL's xmin/autovacuum horizon.
        lease.autocommit = True
        identity = self.identity
        try:
            with lease.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (identity.worker_id,))
                if cursor.fetchone()[0] is not True:
                    raise RuntimeError(f"worker_id {identity.worker_id} already has an active owner")
            return lease
        except Exception:
            lease.close()
            raise

    def _ensure_lease(self) -> None:
        try:
            with self._lease.cursor() as cursor:
                cursor.execute("SELECT 1")
        except psycopg.Error:
            self._lease.close()
            self._lease = self._acquire_lease()

    def _connect(self):
        return psycopg.connect(self.dsn)

    def _verify_account(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT strategy_id, worker_id, timeframe, mode, leverage
                   FROM paper_trading.trading_accounts WHERE account_id = %s""",
                (self.identity.account_id,),
            )
            row = cursor.fetchone()
            expected = (
                self.identity.strategy_id, self.identity.worker_id,
                self.identity.timeframe, self.identity.mode, self.identity.leverage,
            )
            if row is None:
                raise RuntimeError(f"paper account {self.identity.account_id} is not provisioned")
            if tuple(row) != expected:
                raise RuntimeError(f"database identity {tuple(row)!r} does not match worker {expected!r}")

    def sync_tick(
        self,
        state: Any,
        snapshot: dict[str, Any],
        process_id: int,
        funding_events: list[dict[str, Any]] | None = None,
        execution_events: list[dict[str, Any]] | None = None,
        cycle_started_at: datetime | None = None,
        market_data_source: str = "unknown",
        market_data_fresh: bool = False,
        cycle_diagnostics: dict[str, Any] | None = None,
    ) -> None:
        """Atomically publish only this account's current state and heartbeat."""
        ident = self.identity
        positions = snapshot.get("open_positions", [])
        # Validate the authoritative snapshot before _ensure_lease() or any
        # other database call. Duplicate/missing IDs must never partially write.
        validate_position_snapshot(str(ident.account_id), positions)
        self._ensure_lease()
        now = datetime.now(timezone.utc)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE paper_trading.trading_accounts
                   SET cash_balance=%s, margin_used=%s, realized_pnl=%s,
                       unrealized_pnl=%s, funding_pnl=%s, fees=%s,
                       current_equity=%s, ledger_status='OK', updated_at=%s
                   WHERE account_id=%s AND worker_id=%s AND mode=%s""",
                (state.available_balance, state.reserved_margin, state.realized_gross_pnl,
                 snapshot.get("unrealized_pnl", 0), -state.total_funding,
                 state.total_fees + state.total_liquidation_fees,
                 snapshot["current_equity"], now,
                 ident.account_id, ident.worker_id, ident.mode),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("account-scoped balance update affected no row")

            cursor.execute(
                """SELECT trade_id FROM paper_trading.positions
                   WHERE account_id=%s AND mode=%s FOR UPDATE""",
                (ident.account_id, ident.mode),
            )
            stored_trade_ids = {str(row[0]) for row in cursor.fetchall()}
            plan = reconcile_positions(str(ident.account_id), positions, stored_trade_ids)
            for position in plan.upserts:
                cursor.execute(
                    """INSERT INTO paper_trading.positions
                       (account_id,trade_id,strategy_id,worker_id,symbol,quantity,average_entry_price,
                        margin_used,unrealized_pnl,updated_at,mode,side,margin_mode,leverage,
                        isolated_margin,notional,entry_price,entry_time,take_profit_price,
                        stop_loss_price,liquidation_price,entry_fee,entry_order_type,exit_order_type,
                        max_hold_minutes,accrued_funding,last_funding_ts,signal_reason,market_regime,
                        grid_level)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (account_id,trade_id) DO UPDATE SET
                         strategy_id=excluded.strategy_id,worker_id=excluded.worker_id,
                         symbol=excluded.symbol,quantity=excluded.quantity,
                         average_entry_price=excluded.average_entry_price,
                         margin_used=excluded.margin_used,unrealized_pnl=excluded.unrealized_pnl,
                         updated_at=excluded.updated_at,mode=excluded.mode,side=excluded.side,
                         margin_mode=excluded.margin_mode,leverage=excluded.leverage,
                         isolated_margin=excluded.isolated_margin,notional=excluded.notional,
                         entry_price=excluded.entry_price,entry_time=excluded.entry_time,
                         take_profit_price=excluded.take_profit_price,
                         stop_loss_price=excluded.stop_loss_price,
                         liquidation_price=excluded.liquidation_price,entry_fee=excluded.entry_fee,
                         entry_order_type=excluded.entry_order_type,
                         exit_order_type=excluded.exit_order_type,
                         max_hold_minutes=excluded.max_hold_minutes,
                         accrued_funding=excluded.accrued_funding,
                         last_funding_ts=excluded.last_funding_ts,
                         signal_reason=excluded.signal_reason,market_regime=excluded.market_regime,
                         grid_level=excluded.grid_level""",
                    (ident.account_id, position["trade_id"], ident.strategy_id, ident.worker_id, position["symbol"],
                     position["quantity"], position["entry_price"], position["isolated_margin"],
                     position.get("unrealized_gross_pnl", 0), now, ident.mode,
                     position.get("side"), position.get("margin_mode"), position.get("leverage"),
                     position.get("isolated_margin"), position.get("notional"), position.get("entry_price"),
                     position.get("entry_time"), position.get("take_profit_price"),
                     position.get("stop_loss_price"), position.get("liquidation_price"),
                     position.get("entry_fee"), position.get("entry_order_type"),
                     position.get("exit_order_type"), position.get("max_hold_minutes"),
                     position.get("accrued_funding", 0), position.get("last_funding_ts"),
                     position.get("signal_reason"), position.get("market_regime"),
                     position.get("grid_level")),
                )
            for closed_trade_id in plan.deletes:
                cursor.execute(
                    """DELETE FROM paper_trading.positions
                       WHERE account_id=%s AND mode=%s AND trade_id=%s""",
                    (ident.account_id, ident.mode, closed_trade_id),
                )

            for event in funding_events or []:
                cursor.execute(
                    """INSERT INTO paper_trading.funding_events
                       (account_id,strategy_id,worker_id,symbol,funding_timestamp,
                        funding_rate,funding_pnl,mode)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (account_id,symbol,funding_timestamp) DO NOTHING""",
                    (ident.account_id, ident.strategy_id, ident.worker_id, event["symbol"],
                     event["funding_timestamp"], event["funding_rate"],
                     event["funding_pnl"], ident.mode),
                )

            last_trade_at = None
            for event in execution_events or []:
                event_type = event.get("event")
                if event_type not in {"position_opened", "position_closed"}:
                    continue
                is_open = event_type == "position_opened"
                trade_id = str(event["trade_id"])
                scope = "open" if is_open else "close"
                order_id = f"{trade_id}:{scope}"
                raw_side = str(event["side"]).lower()
                side = ("BUY" if raw_side == "long" else "SELL") if is_open else (
                    "SELL" if raw_side == "long" else "BUY"
                )
                price = event["entry_price"] if is_open else event["exit_price"]
                fee = event.get("entry_fee", 0) if is_open else event.get("exit_fee", 0)
                timestamp = event["timestamp"]
                last_trade_at = timestamp
                cursor.execute(
                    """INSERT INTO paper_trading.orders
                       (account_id,strategy_id,worker_id,exchange_order_id,symbol,side,
                        leverage,status,quantity,created_at,mode)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'FILLED',%s,%s,%s)
                       ON CONFLICT (account_id,exchange_order_id) DO NOTHING""",
                    (ident.account_id, ident.strategy_id, ident.worker_id, order_id,
                     event["symbol"], side, event["leverage"], abs(event["quantity"]),
                     timestamp, ident.mode),
                )
                cursor.execute(
                    """INSERT INTO paper_trading.fills
                       (account_id,strategy_id,worker_id,exchange_fill_id,exchange_order_id,
                        symbol,side,quantity,price,fee,filled_at,mode)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (account_id,exchange_fill_id) DO NOTHING""",
                    (ident.account_id, ident.strategy_id, ident.worker_id, order_id,
                     order_id, event["symbol"], side, abs(event["quantity"]), price,
                     fee, timestamp, ident.mode),
                )

            cursor.execute(
                """INSERT INTO paper_trading.equity_history
                   (account_id,strategy_id,worker_id,marked_at,cash_balance,margin_used,
                    realized_pnl,unrealized_pnl,funding_pnl,fees,equity,mode)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (account_id,marked_at) DO NOTHING""",
                (ident.account_id, ident.strategy_id, ident.worker_id, snapshot["timestamp"],
                 state.available_balance, state.reserved_margin, state.realized_gross_pnl,
                 snapshot.get("unrealized_pnl", 0), -state.total_funding,
                 state.total_fees + state.total_liquidation_fees,
                 snapshot["current_equity"], ident.mode),
            )
            cursor.execute(
                """INSERT INTO paper_trading.worker_heartbeats
                   (account_id,strategy_id,worker_id,process_id,last_seen_at,last_trade_at,risk_state,mode)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                   ON CONFLICT (account_id) DO UPDATE SET
                     process_id=excluded.process_id,last_seen_at=excluded.last_seen_at,
                     last_trade_at=COALESCE(excluded.last_trade_at,worker_heartbeats.last_trade_at),
                     risk_state=excluded.risk_state,mode=excluded.mode""",
                (ident.account_id, ident.strategy_id, ident.worker_id, process_id, now,
                 last_trade_at, json.dumps(snapshot.get("risk_state", {})), ident.mode),
            )
            executions = execution_events or []
            diagnostics = cycle_diagnostics or {}
            cursor.execute(
                """INSERT INTO paper_trading.paper_cycle_events
                   (id,account_id,worker_id,strategy_id,timeframe,cycle_started_at,cycle_completed_at,
                    market_data_source,market_data_fresh,orders_created,fills_created,trades_closed,
                    realized_pnl,unrealized_pnl,fees,funding_pnl,ending_equity,ledger_reconciled,status,
                    signal_score,entry_threshold,market_data_age,volatility,spread,risk_rejection_reason,
                    strategy_rejection_reason,order_rejection_reason,decision_funnel)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,'COMPLETED',%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (uuid4(), ident.account_id, ident.worker_id, ident.strategy_id, ident.timeframe,
                 cycle_started_at or now, now, market_data_source, market_data_fresh,
                 sum(1 for e in executions if e.get("event") == "position_opened"),
                 sum(1 for e in executions if e.get("event") == "position_opened"),
                 sum(1 for e in executions if e.get("event") == "position_closed"),
                 state.realized_gross_pnl, snapshot.get("unrealized_pnl", 0),
                 state.total_fees + state.total_liquidation_fees, -state.total_funding,
                 snapshot["current_equity"], diagnostics.get("signal_score"),
                 diagnostics.get("entry_threshold"), diagnostics.get("market_data_age"),
                 diagnostics.get("volatility"), diagnostics.get("spread"),
                 diagnostics.get("risk_rejection_reason"), diagnostics.get("strategy_rejection_reason"),
                 diagnostics.get("order_rejection_reason"),
                 json.dumps(diagnostics.get("decision_funnel", {}), sort_keys=True)),
            )

    def close(self) -> None:
        self._lease.close()

    def heartbeat(self, process_id: int) -> None:
        """Refresh liveness independently of the slower strategy timeframe."""
        self._ensure_lease()
        ident = self.identity
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE paper_trading.worker_heartbeats
                      SET process_id=%s,last_seen_at=%s
                    WHERE account_id=%s AND worker_id=%s AND mode=%s""",
                (process_id, datetime.now(timezone.utc), ident.account_id,
                 ident.worker_id, ident.mode),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("account-scoped heartbeat update affected no row")
