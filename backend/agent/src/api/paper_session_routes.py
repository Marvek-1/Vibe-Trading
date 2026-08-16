"""Read-only HTTP routes for simulated paper-trading sessions.

Mounted by ``agent/api_server.py`` via ``register_paper_session_routes(app)``.

These routes only ever read from ``agent/paper_sessions/<id>/`` -- the same
files ``paper_session.py``'s CLI writes (``session.json`` static config,
``book.json`` current positions/cash, ``marks.jsonl`` equity snapshots,
``trades.jsonl`` executed trades). No route here starts, stops, or mutates a
session; that stays a deliberate CLI/background-process action (see
``paper_session.py``), not something the frontend can trigger. The frontend
polls this for a live view of a session that is not the real-money
live-runner (``src/live/``) and never places a broker order.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

AuthDep = Callable[..., Any]

PAPER_SESSIONS_DIR = Path(__file__).resolve().parents[2] / "paper_sessions"
REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "paper_sessions_registry.json"
_SAFE_SESSION_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_-]+$")

_VALID_SCOPES = {"active", "archived", "all"}


def _postgres_account_view(account_id: str) -> dict[str, Any] | None:
    """Read one account projection; every child-table lookup is account scoped."""
    try:
        from uuid import UUID

        import psycopg

        parsed_id = UUID(account_id)
        dsn = os.getenv("VIBE_PAPER_DATABASE_URL", "dbname=idim_ikang port=5433")
        with psycopg.connect(dsn, connect_timeout=2) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT a.strategy_id,a.worker_id,a.timeframe,a.mode,a.leverage,
                          a.initial_capital,a.cash_balance,a.margin_used,a.realized_pnl,
                          a.unrealized_pnl,a.funding_pnl,a.fees,a.current_equity,
                          a.ledger_status,h.last_seen_at,h.last_trade_at,h.risk_state,
                          (SELECT count(*) FROM paper_trading.positions p
                            WHERE p.account_id=a.account_id AND p.mode=a.mode),
                          (SELECT c.market_data_source FROM paper_trading.paper_cycle_events c
                            WHERE c.account_id=a.account_id
                            ORDER BY c.cycle_completed_at DESC LIMIT 1),
                          (SELECT c.cycle_completed_at FROM paper_trading.paper_cycle_events c
                            WHERE c.account_id=a.account_id
                            ORDER BY c.cycle_completed_at DESC LIMIT 1)
                     FROM paper_trading.trading_accounts a
                     LEFT JOIN paper_trading.worker_heartbeats h
                       ON h.account_id=a.account_id AND h.mode=a.mode
                    WHERE a.account_id=%s""",
                (parsed_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "account_id": account_id,
            "strategy_id": row[0], "worker_id": row[1], "timeframe": row[2],
            "mode": row[3], "leverage": row[4], "initial_capital": float(row[5]),
            "cash_available": float(row[6]), "margin_used": float(row[7]),
            "realized_pnl": float(row[8]), "unrealized_pnl": float(row[9]),
            "funding_pnl": float(row[10]), "fees": float(row[11]),
            "current_equity": float(row[12]), "ledger_status": row[13],
            "last_heartbeat": row[14].isoformat() if row[14] else None,
            "last_trade": row[15].isoformat() if row[15] else None,
            "risk_state": row[16] or {}, "open_positions": row[17],
            # Persisted by grid_futures_runner from the actual price-fetch
            # branch that answered (binance/okx/gate) -- never inferred here.
            "market_data_source": row[18],
            "last_cycle_completed_at": row[19].isoformat() if row[19] else None,
        }
    except Exception:  # database telemetry must not take down the read-only dashboard
        return None


def _validate_session_id(session_id: str) -> None:
    if not _SAFE_SESSION_ID_RE.fullmatch(session_id or ""):
        raise HTTPException(status_code=400, detail="invalid session_id")


def _load_registry() -> tuple[frozenset[str], frozenset[str], Optional[str]]:
    """Load the active/archived paper-session registry.

    Fails closed: any missing file, invalid JSON, or malformed schema
    returns two empty sets (every session then classifies as "unknown",
    never silently "active") plus a human-readable error string the
    caller can surface instead of guessing.
    """
    try:
        raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return frozenset(), frozenset(), "registry file not found"
    except json.JSONDecodeError as exc:
        return frozenset(), frozenset(), f"registry file is not valid JSON: {exc}"

    active = raw.get("active_sessions")
    archived = raw.get("archived_sessions")
    if not isinstance(active, list) or not isinstance(archived, list):
        return frozenset(), frozenset(), "registry missing active_sessions/archived_sessions arrays"

    return frozenset(active), frozenset(archived), None


def _classify_session(session_id: str, active_ids: frozenset[str], archived_ids: frozenset[str]) -> str:
    """Never infers -- a session is only "active" if the registry says so."""
    if session_id in archived_ids:
        return "archived"
    if session_id in active_ids:
        return "active"
    return "unknown"


def _registry_status(classification: str, marks: list[dict[str, Any]]) -> str:
    """Status derived from the registry classification plus mark freshness.

    An archived session can never report "running", even if a stale
    .heartbeat file from before its retirement is still sitting on disk.
    """
    if classification == "archived":
        return "archived"
    if classification == "unknown":
        return "unknown"

    from paper_session import RUNTIME_STALE_AFTER, _parse_iso

    if not marks:
        return "stale"
    try:
        age = datetime.now(timezone.utc) - _parse_iso(marks[-1]["timestamp"])
    except Exception:
        return "stale"
    return "running" if age < RUNTIME_STALE_AFTER else "stale"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _summarize(session_dir: Path, classification: str = "unknown") -> dict[str, Any]:
    # Always load from files for critical metrics — files are the source of truth.
    from paper_session import verify_receipted_file, _check_heartbeat

    session_path = session_dir / "session.json"
    session_intact = verify_receipted_file(session_path)
    session = json.loads(session_path.read_text(encoding="utf-8"))

    book_path = session_dir / "book.json"
    if book_path.exists():
        book_intact = verify_receipted_file(book_path)
        book = json.loads(book_path.read_text(encoding="utf-8"))
    elif "positions" in session:
        book = {
            "positions": session["positions"],
            "cash_remaining": session.get("cash_remaining", 0.0),
            "last_rebalance_time": session.get("entry_time"),
        }
        book_intact = True
    else:
        book = None
        book_intact = True

    marks = _read_jsonl(session_dir / "marks.jsonl")
    trades = _read_jsonl(session_dir / "trades.jsonl")
    latest = marks[-1] if marks else None

    # File-first equity: prefer latest mark, fall back to book cash.
    if latest:
        equity = float(latest["equity"])
    elif book:
        equity = float(book.get("cash_remaining", 0.0))
    else:
        equity = 0.0

    initial_cash = float(session.get("initial_cash", 0.0))
    pnl = equity - initial_cash
    return_pct = (pnl / initial_cash) * 100.0 if initial_cash else 0.0

    peak = float("-inf")
    max_drawdown = 0.0
    equity_curve = []
    for m in marks:
        e = float(m["equity"])
        peak = max(peak, e)
        drawdown = (e - peak) / peak if peak > 0 else 0.0
        max_drawdown = min(max_drawdown, drawdown)
        equity_curve.append({"time": m["timestamp"], "equity": e, "drawdown": drawdown})

    from paper_session import compute_trade_stats, compute_session_status

    trade_stats = compute_trade_stats(trades)
    status = compute_session_status(session_dir)

    # Override runtime_status with heartbeat if available (more precise than marks age)
    hb_status = _check_heartbeat(session_dir)
    if hb_status == "running":
        status["runtime_status"] = "running"
        status["active"] = True

    return {
        "session_id": session_dir.name,
        "session": session,
        "book": book,
        "mark_count": len(marks),
        "latest_mark": latest,
        "trade_count": len(trades),
        "recent_trades": trade_stats["trades"][-50:],
        "trade_stats": {"overall": trade_stats["overall"], "by_symbol": trade_stats["by_symbol"]},
        "equity_curve": equity_curve,
        "max_drawdown": max_drawdown,
        "equity": equity,
        "pnl": pnl,
        "return_pct": return_pct,
        "tampered": not session_intact or not book_intact,
        "classification": classification,
        "status": _registry_status(classification, marks),
        **status,
    }


def _summarize_db_session(session: dict, trades: list[dict], marks: list[dict]) -> dict:
    """Build a PaperSessionSummary from data stored in the paper_store DB."""
    symbols = session.get("symbols", [])

    # Entry prices from the first BUY trade per symbol
    entry_prices: dict[str, float] = {}
    for t in trades:
        if t.get("side") == "BUY" and t.get("symbol") in symbols and t["symbol"] not in entry_prices:
            entry_prices[t["symbol"]] = float(t["price"])
    session["entry_prices"] = entry_prices

    latest = marks[-1] if marks else None
    book = {
        "positions": {sym: 0.0 for sym in symbols},
        "cash_remaining": float(latest["cash_remaining"]) if latest else 0.0,
        "last_rebalance_time": session.get("entry_time", ""),
    }

    peak = float("-inf")
    max_drawdown = 0.0
    equity_curve = []
    for m in marks:
        equity = float(m.get("equity", 0.0))
        peak = max(peak, equity)
        drawdown = (equity - peak) / peak if peak > 0 else 0.0
        max_drawdown = min(max_drawdown, drawdown)
        equity_curve.append({"time": m["timestamp"], "equity": equity, "drawdown": drawdown})

    from paper_session import compute_trade_stats

    trade_stats = compute_trade_stats(trades)
    # DB sessions are historical; not live
    status = {
        "runtime_status": "stopped",
        "analysis_status": "valid",
        "accounting_status": "OK",
        "accounting_schema_version": 2,
        "session_role": "candidate",
        "regimen": "backtest",
        "active": False,
    }

    return {
        "session_id": session.get("session_id", session.get("id", "")),
        "session": session,
        "book": book,
        "mark_count": len(marks),
        "latest_mark": latest,
        "trade_count": len(trades),
        "recent_trades": trade_stats["trades"][-50:],
        "trade_stats": {"overall": trade_stats["overall"], "by_symbol": trade_stats["by_symbol"]},
        "equity_curve": equity_curve,
        "max_drawdown": max_drawdown,
        "classification": "historical",
        "status": "archived",
        **status,
    }


def _detect_session_schema(session_dir: Path) -> Optional[str]:
    """Two on-disk session shapes live under paper_sessions/: paper_session.py's
    CLI (session.json/book.json/marks.jsonl/trades.jsonl) and
    FuturesPaperEngine's (session_config.json/account.json/marks.jsonl/
    trades.jsonl). ``.is_file()`` -- not ``.exists()`` -- so a stray
    directory of the same name never misclassifies as a manifest.
    """
    if (session_dir / "session.json").is_file():
        return "paper_session"
    if (session_dir / "session_config.json").is_file():
        return "futures_paper"
    return None


def _futures_position_book(state: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Normalize FuturesPaperEngine's positions-by-trade_id state into the
    same {positions: {symbol: signed_qty}, position_metadata: {...}} shape
    paper_session.py's book.json already uses, so the frontend's existing
    position-table/margin-allocation rendering needs no changes.
    """
    if state is None:
        return None
    positions_qty: dict[str, float] = {}
    position_metadata: dict[str, Any] = {}
    for pos in state.get("positions", {}).values():
        symbol = pos["symbol"]
        direction = 1 if pos["side"] == "long" else -1
        positions_qty[symbol] = direction * pos["quantity"]
        position_metadata[symbol] = {
            "symbol": symbol,
            "qty": pos["quantity"],
            "entry_price": pos["entry_price"],
            "entry_time": pos["entry_time"],
            "direction": direction,
            "high_water_mark": pos["high_water_mark"],
            "low_water_mark": pos["low_water_mark"],
            "leverage": pos["leverage"],
            "margin": pos["isolated_margin"],
            "margin_mode": pos["margin_mode"],
            "liquidation_price": pos["liquidation_price"],
            "take_profit_price": pos.get("take_profit_price"),
            "stop_loss_price": pos.get("stop_loss_price"),
        }
    return {
        "positions": positions_qty,
        "cash_remaining": state.get("wallet_balance", 0.0) - state.get("reserved_margin", 0.0),
        "last_rebalance_time": None,
        "position_metadata": position_metadata,
    }


def _futures_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Win/loss stats straight from FuturesPaperEngine's ClosedTrade rows.

    Each row's net_pnl is already the final, fee/funding-inclusive result --
    unlike paper_session.py's compute_trade_stats, no signed-position
    cost-basis reconstruction across partial fills is needed here.
    """
    if not trades:
        return {"overall": None, "by_symbol": {}}
    wins = [t for t in trades if t.get("net_pnl", 0.0) > 0]
    losses = [t for t in trades if t.get("net_pnl", 0.0) <= 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = sum(-t["net_pnl"] for t in losses)
    overall = {
        "trade_count": len(trades),
        "win_rate": len(wins) / len(trades),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_win": (gross_win / len(wins)) if wins else None,
        "avg_loss": (gross_loss / len(losses)) if losses else None,
    }
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)
    return {"overall": overall, "by_symbol": by_symbol}


def _load_futures_paper_session(session_dir: Path, classification: str = "unknown") -> dict[str, Any]:
    """Normalize a FuturesPaperEngine session into the same response shape
    _summarize() produces for a paper_session.py session.

    Never infers a position, trade, or equity value from session_config.json
    -- configuration describes what an account *would* trade with, not what
    it has done. An account with no account.json/marks.jsonl yet (created
    but never launched, e.g. grid_futures_10x_v2 today) reports empty
    positions, zero trades, and runtime_status "not_started".
    """
    config = json.loads((session_dir / "session_config.json").read_text(encoding="utf-8"))

    universe_path = session_dir / config.get("universe_path", "")
    try:
        symbols = json.loads(universe_path.read_text(encoding="utf-8")).get("symbols", [])
    except (OSError, ValueError):
        symbols = []

    state_path = session_dir / "account.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None
    book = _futures_position_book(state)

    marks = _read_jsonl(session_dir / "marks.jsonl")
    trades = _read_jsonl(session_dir / "trades.jsonl")

    def _mark_equity(m: dict[str, Any]) -> float:
        return float(m.get("current_equity", m.get("equity", 0.0)))

    latest = None
    if marks:
        raw_latest = marks[-1]
        latest = {**raw_latest, "equity": _mark_equity(raw_latest)}

    initial_cash = float(config.get("initial_balance", 0.0))
    if latest:
        equity = latest["equity"]
    elif state:
        equity = float(state.get("wallet_balance", initial_cash))
    else:
        equity = initial_cash
    pnl = equity - initial_cash
    return_pct = (pnl / initial_cash) * 100.0 if initial_cash else 0.0

    peak = float("-inf")
    max_drawdown = 0.0
    equity_curve = []
    for m in marks:
        e = _mark_equity(m)
        peak = max(peak, e)
        drawdown = (e - peak) / peak if peak > 0 else 0.0
        max_drawdown = min(max_drawdown, drawdown)
        equity_curve.append({"time": m["timestamp"], "equity": e, "drawdown": drawdown})

    trade_stats = _futures_trade_stats(trades)

    never_launched = not marks and (state is None or state.get("opened_trades", 0) == 0)
    if never_launched:
        runtime_status = "not_started"
    elif not marks:
        runtime_status = "stopped"
    else:
        from paper_session import RUNTIME_STALE_AFTER, _parse_iso

        try:
            age = datetime.now(timezone.utc) - _parse_iso(marks[-1]["timestamp"])
            runtime_status = "running" if age < RUNTIME_STALE_AFTER else "stopped"
        except Exception:
            runtime_status = "stopped"

    status = "archived" if classification == "archived" else "unknown" if classification == "unknown" else runtime_status

    account_id = str(config.get("account_id", session_dir.name))
    database_account = _postgres_account_view(account_id)
    if database_account is None:
        # The dashboard commonly runs in Docker while the isolated paper
        # workers and PostgreSQL run on the host.  Session files are bind
        # mounted, so preserve useful liveness/account telemetry even when
        # that container cannot open the host-only database socket.
        heartbeat_path = session_dir / ".heartbeat"
        try:
            last_heartbeat = heartbeat_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            last_heartbeat = None
        database_account = {
            "account_id": account_id,
            "strategy_id": config.get("strategy_id"),
            "worker_id": config.get("worker_id"),
            "timeframe": config.get("timeframe"),
            "mode": config.get("mode", "paper"),
            "leverage": config.get("leverage"),
            "initial_capital": initial_cash,
            "cash_available": (state or {}).get("wallet_balance", initial_cash)
            - (state or {}).get("reserved_margin", 0.0),
            "margin_used": (state or {}).get("reserved_margin", 0.0),
            "realized_pnl": (state or {}).get("realized_net_pnl", 0.0),
            "unrealized_pnl": (latest or {}).get("unrealized_pnl", 0.0),
            "funding_pnl": -(state or {}).get("funding_paid", 0.0),
            "fees": (state or {}).get("fees_paid", 0.0),
            "current_equity": equity,
            "ledger_status": "OK",
            "last_heartbeat": last_heartbeat,
            "last_trade": trades[-1].get("exit_time") if trades else None,
            "risk_state": {},
            "open_positions": len((state or {}).get("positions", {})),
            "market_data_source": (latest or {}).get("market_data_source"),
            "last_cycle_completed_at": (latest or {}).get("timestamp"),
            "telemetry_source": "session_files",
        }
    # A strategy may intentionally mark at 10m/15m intervals. Its database
    # heartbeat is the liveness authority between marks, not the mark age.
    if database_account and database_account.get("last_heartbeat"):
        try:
            heartbeat_at = datetime.fromisoformat(database_account["last_heartbeat"])
            if datetime.now(timezone.utc) - heartbeat_at <= timedelta(seconds=45):
                runtime_status = "running"
                status = "running"
        except (TypeError, ValueError):
            pass
    session_view = {
        "strategy_type": config.get("strategy_type", "futures_paper_engine"),
        "account_id": account_id,
        "strategy_id": database_account.get("strategy_id") if database_account else None,
        "worker_id": database_account.get("worker_id") if database_account else None,
        "timeframe": database_account.get("timeframe") if database_account else None,
        "symbols": symbols,
        "initial_cash": initial_cash,
        "accounting_status": "OK",
        "accounting_schema_version": (state or {}).get("schema_version", 1),
        "risk_config": {
            "leverage": config.get("leverage"),
            "margin_mode": config.get("margin_mode", "isolated"),
            "fixed_margin_per_trade": config.get("margin_per_trade"),
        },
        "config_status": config.get("status"),
    }

    return {
        "session_id": session_dir.name,
        "session": session_view,
        "book": book,
        "mark_count": len(marks),
        "latest_mark": latest,
        "trade_count": len(trades),
        "recent_trades": trades[-50:],
        "trade_stats": trade_stats,
        "equity_curve": equity_curve,
        "max_drawdown": max_drawdown,
        "equity": equity,
        "pnl": pnl,
        "return_pct": return_pct,
        "tampered": False,
        "classification": classification,
        "status": status,
        "runtime_status": runtime_status,
        "analysis_status": "valid",
        "accounting_status": "OK",
        "accounting_schema_version": (state or {}).get("schema_version", 1),
        "session_role": None,
        "regimen": None,
        "active": runtime_status == "running",
        "database_account": database_account,
    }


def register_paper_session_routes(
    app: FastAPI,
    require_auth: Optional[AuthDep] = None,
) -> None:
    auth_dependencies = [Depends(require_auth)] if require_auth is not None else []

    @app.get("/paper-trading/notifications", dependencies=auth_dependencies)
    async def get_paper_trading_notifications(after: Optional[str] = None):
        """Read-only notification feed for paper-dashboard toast polling."""
        try:
            import psycopg
            dsn = os.getenv("VIBE_PAPER_DATABASE_URL", "dbname=idim_ikang port=5433")
            query = """SELECT id,worker_id,event_type,severity,title,message,created_at
                         FROM paper_trading.trading_notifications
                         WHERE (%s::timestamptz IS NULL OR created_at > %s::timestamptz)
                         ORDER BY created_at ASC LIMIT 100"""
            with psycopg.connect(dsn, connect_timeout=2) as conn, conn.cursor() as cur:
                cur.execute(query, (after, after))
                rows = cur.fetchall()
            return [{"id": str(r[0]), "worker_id": r[1], "event_type": r[2],
                     "severity": r[3], "title": r[4], "message": r[5],
                     "created_at": r[6].isoformat()} for r in rows]
        except Exception as exc:
            raise HTTPException(status_code=503, detail="paper notification feed unavailable") from exc
    """Mount the read-only paper-session routes onto ``app``."""
    if require_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:  # pragma: no cover
            raise RuntimeError(
                "register_paper_session_routes: api_server module not in "
                "sys.modules; pass require_auth explicitly"
            )
        require_auth = host.require_auth

    @app.get("/paper-sessions", dependencies=[Depends(require_auth)])
    async def list_paper_sessions(scope: str = "active"):
        """List paper sessions, including backtest sessions stored in the database.

        ``scope`` controls the active/archived registry split
        (``config/paper_sessions_registry.json``):

        - ``active`` (default) -- registry-listed active sessions only.
        - ``archived`` -- registry-listed archived sessions only.
        - ``all`` -- both groups.

        A filesystem session not covered by the registry (e.g. a new
        strategy family the registry predates) is classified "unknown" and
        is never excluded by scope -- only registry-covered sessions are
        scope-filtered, so this stays additive rather than hiding sessions
        nobody has classified yet. DB-only sessions are the exception:
        historical, never registry-governed, so scope never hides them
        either, but they're only merged in on ``scope=all``.

        If the registry file is missing or malformed, this fails closed:
        every filesystem/db session is skipped and an empty list is
        returned, rather than risk showing a retired session as active.
        """
        if scope not in _VALID_SCOPES:
            raise HTTPException(status_code=400, detail=f"scope must be one of: {sorted(_VALID_SCOPES)}")

        active_ids, archived_ids, registry_error = _load_registry()
        if registry_error is not None:
            return []

        results = []
        if not PAPER_SESSIONS_DIR.exists():
            return []
        # Filesystem sessions -- either paper_session.py's (session.json) or
        # FuturesPaperEngine's (session_config.json) manifest shape.
        existing_ids = set()
        candidate_dirs = sorted(
            (d for d in PAPER_SESSIONS_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")),
            key=lambda d: d.name,
            reverse=True,
        )
        for d in candidate_dirs:
            schema = _detect_session_schema(d)
            if schema is None:
                continue
            existing_ids.add(d.name)
            classification = _classify_session(d.name, active_ids, archived_ids)
            if classification != "unknown" and scope != "all" and classification != scope:
                continue
            if schema == "paper_session":
                results.append(_summarize(d, classification=classification))
            else:
                results.append(_load_futures_paper_session(d, classification=classification))

        # DB-only backtests are internal-review data and only belong to the
        # explicit all-sessions scope, never the active dashboard.
        try:
            if scope != "all":
                return results
            from paper_store import get_store
            store = get_store()
            db_sessions = store.list_sessions(strategy_type="per_symbol_isolated_backtest")
            for s in db_sessions:
                sid = s["session_id"]
                if sid in existing_ids:
                    continue
                trades = store.list_trades(sid)
                marks = store.list_marks(sid)
                results.append(_summarize_db_session(s, trades, marks))
                existing_ids.add(sid)
        except Exception:
            # If paper_store isn't available or fails, just return filesystem sessions
            pass

        return results

    @app.get("/paper-sessions/registry-status", dependencies=[Depends(require_auth)])
    async def get_paper_session_registry_status():
        """Diagnostics for the active/archived registry itself, separate from
        the main listing so a broken registry is visible rather than just
        silently emptying /paper-sessions.
        """
        active_ids, archived_ids, registry_error = _load_registry()
        if registry_error is not None:
            return {"registry_status": "error", "detail": registry_error, "active_sessions": [], "archived_sessions": []}
        return {
            "registry_status": "ok",
            "detail": None,
            "active_sessions": sorted(active_ids),
            "archived_sessions": sorted(archived_ids),
        }

    @app.get("/paper-sessions/provider-health", dependencies=[Depends(require_auth)])
    async def get_paper_provider_health():
        """Probe every credential-free market provider independently."""
        from market_provider_health import get_market_provider_health

        return await run_in_threadpool(get_market_provider_health)

    @app.get("/paper-sessions/decision-health", dependencies=[Depends(require_auth)])
    async def get_paper_decision_health():
        """Return the strategy-to-fill funnel from committed cycle evidence.

        This is deliberately separate from process and provider health. A
        worker can be alive, receive a fresh quote, and still have no valid
        strategy candidate or be blocked by an execution policy.
        """
        try:
            import psycopg

            dsn = os.getenv("VIBE_PAPER_DATABASE_URL", "dbname=idim_ikang port=5433")
            with psycopg.connect(dsn, connect_timeout=2) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """WITH recent AS (
                           SELECT *
                             FROM paper_trading.paper_cycle_events
                            WHERE cycle_completed_at > now() - interval '24 hours'
                         ), latest AS (
                           SELECT DISTINCT ON (worker_id)
                                  worker_id, cycle_completed_at, market_data_fresh,
                                  strategy_rejection_reason, risk_rejection_reason,
                                  order_rejection_reason, decision_funnel
                             FROM recent
                            ORDER BY worker_id, cycle_completed_at DESC
                         )
                         SELECT l.worker_id,l.cycle_completed_at,l.market_data_fresh,
                                l.strategy_rejection_reason,l.risk_rejection_reason,
                                l.order_rejection_reason,l.decision_funnel,
                                coalesce(sum((r.decision_funnel->>'cycles_completed')::bigint), 0),
                                coalesce(sum((r.decision_funnel->>'signals_evaluated')::bigint), 0),
                                coalesce(sum((r.decision_funnel->>'signals_true')::bigint), 0),
                                coalesce(sum((r.decision_funnel->>'entries_requested')::bigint), 0),
                                coalesce(sum((r.decision_funnel->>'paper_orders_filled')::bigint), 0),
                                coalesce(sum((r.decision_funnel->>'positions_closed')::bigint), 0)
                           FROM latest l
                           JOIN recent r USING (worker_id)
                          GROUP BY l.worker_id,l.cycle_completed_at,l.market_data_fresh,
                                   l.strategy_rejection_reason,l.risk_rejection_reason,
                                   l.order_rejection_reason,l.decision_funnel
                          ORDER BY l.worker_id"""
                )
                rows = cursor.fetchall()
        except Exception as exc:
            return {"status": "error", "detail": str(exc), "window_hours": 24, "workers": []}

        workers = []
        for row in rows:
            workers.append({
                "worker_id": row[0],
                "last_cycle_at": row[1].isoformat() if row[1] else None,
                "market_data_fresh": bool(row[2]),
                "latest_rejections": {
                    "strategy": row[3], "risk": row[4], "order": row[5],
                },
                "latest_funnel": row[6] or {},
                "window": {
                    "cycles_completed": int(row[7]),
                    "signals_evaluated": int(row[8]),
                    "signals_true": int(row[9]),
                    "entries_requested": int(row[10]),
                    "paper_orders_filled": int(row[11]),
                    "positions_closed": int(row[12]),
                },
            })
        return {"status": "ok", "detail": None, "window_hours": 24, "workers": workers}

    @app.get("/paper-sessions/shadow-comparison", dependencies=[Depends(require_auth)])
    async def get_shadow_comparison():
        """Pair up control/$-threshold-candidate sessions by rebalance regimen.

        Only schema-v2 sessions with session_role control/candidate
        participate -- historical (schema-v1) sessions are never paired in.
        """
        if not PAPER_SESSIONS_DIR.exists():
            return []

        from paper_session import build_shadow_comparison

        return await run_in_threadpool(build_shadow_comparison, PAPER_SESSIONS_DIR)

    @app.get("/paper-sessions/shadow-comparison.csv", dependencies=[Depends(require_auth)])
    async def get_shadow_comparison_csv():
        """Flat CSV of the same shadow-comparison rows, one per regimen."""
        from paper_session import build_shadow_comparison

        comparisons = await run_in_threadpool(build_shadow_comparison, PAPER_SESSIONS_DIR)

        import csv
        import io

        buffer = io.StringIO()
        fields = [
            "regimen", "control_session_id", "candidate_session_id",
            "control_net_return", "candidate_net_return", "delta_net_return",
            "control_total_fees", "candidate_total_fees", "delta_total_fees",
            "control_trade_count", "candidate_trade_count", "delta_trade_count",
            "control_turnover", "candidate_turnover", "delta_turnover",
            "control_max_drawdown", "candidate_max_drawdown", "delta_max_drawdown",
            "control_reconciled", "candidate_reconciled",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        for row in comparisons:
            control = row.get("control") or {}
            candidate = row.get("candidate") or {}
            delta = row.get("delta") or {}
            writer.writerow({
                "regimen": row["regimen"],
                "control_session_id": row["control_session_id"],
                "candidate_session_id": row["candidate_session_id"],
                "control_net_return": control.get("net_return"),
                "candidate_net_return": candidate.get("net_return"),
                "delta_net_return": delta.get("net_return"),
                "control_total_fees": control.get("total_fees"),
                "candidate_total_fees": candidate.get("total_fees"),
                "delta_total_fees": delta.get("total_fees"),
                "control_trade_count": control.get("trade_count"),
                "candidate_trade_count": candidate.get("trade_count"),
                "delta_trade_count": delta.get("trade_count"),
                "control_turnover": control.get("turnover"),
                "candidate_turnover": candidate.get("turnover"),
                "delta_turnover": delta.get("turnover"),
                "control_max_drawdown": control.get("max_drawdown"),
                "candidate_max_drawdown": candidate.get("max_drawdown"),
                "delta_max_drawdown": delta.get("max_drawdown"),
                "control_reconciled": control.get("reconciled"),
                "candidate_reconciled": candidate.get("reconciled"),
            })

        from fastapi.responses import Response

        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=shadow-comparison.csv"},
        )

    @app.get("/paper-sessions/{session_id}", dependencies=[Depends(require_auth)])
    async def get_paper_session(session_id: str):
        """Full detail for one paper session: config, every mark, and an equity curve."""
        _validate_session_id(session_id)
        session_dir = PAPER_SESSIONS_DIR / session_id
        schema = _detect_session_schema(session_dir) if session_dir.exists() else None
        if schema is None:
            raise HTTPException(status_code=404, detail=f"paper session {session_id!r} not found")
        active_ids, archived_ids, registry_error = _load_registry()
        classification = "unknown" if registry_error is not None else _classify_session(session_id, active_ids, archived_ids)
        if schema == "paper_session":
            return _summarize(session_dir, classification=classification)
        return _load_futures_paper_session(session_dir, classification=classification)

    @app.get("/paper-sessions/{session_id}/live-prices", dependencies=[Depends(require_auth)])
    async def get_paper_session_live_prices(session_id: str):
        """Current live prices for a session's symbols -- display only.

        Never reads or writes marks.jsonl/trades.jsonl/book.json. This is a
        fast, frequent poll (every few seconds) purely to show price
        movement; the receipted ledger only updates on its own schedule via
        the background loop.
        """
        _validate_session_id(session_id)
        session_dir = PAPER_SESSIONS_DIR / session_id
        session_path = session_dir / "session.json"
        if not session_path.exists():
            raise HTTPException(status_code=404, detail=f"paper session {session_id!r} not found")
        session = json.loads(session_path.read_text(encoding="utf-8"))

        from paper_session import fetch_last_prices_fast  # agent/paper_session.py, top-level module

        try:
            prices = await run_in_threadpool(fetch_last_prices_fast, session["symbols"])
        except Exception as exc:  # noqa: BLE001 - upstream exchange call, surface as 502
            raise HTTPException(status_code=502, detail=f"live price fetch failed: {exc}") from exc

        import datetime as _dt

        return {"prices": prices, "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat()}

    @app.get("/paper-sessions/{session_id}/diagnostics", dependencies=[Depends(require_auth)])
    async def get_paper_session_diagnostics(session_id: str):
        """Detailed fee-aware diagnostics from the receipted paper ledger."""
        _validate_session_id(session_id)
        session_dir = PAPER_SESSIONS_DIR / session_id
        if not session_dir.exists() or not (session_dir / "session.json").exists():
            raise HTTPException(status_code=404, detail=f"paper session {session_id!r} not found")

        from paper_session import compute_session_diagnostics

        return await run_in_threadpool(compute_session_diagnostics, session_dir)

    @app.get("/paper-sessions/{session_id}/diagnostics.csv", dependencies=[Depends(require_auth)])
    async def get_paper_session_diagnostics_csv(session_id: str):
        """CSV export of closed paper trades for offline audit."""
        _validate_session_id(session_id)
        session_dir = PAPER_SESSIONS_DIR / session_id
        if not session_dir.exists() or not (session_dir / "session.json").exists():
            raise HTTPException(status_code=404, detail=f"paper session {session_id!r} not found")

        from paper_session import export_closed_trade_diagnostics_csv

        export_path = session_dir / "closed_trade_diagnostics.csv"
        await run_in_threadpool(export_closed_trade_diagnostics_csv, session_dir, export_path)
        return FileResponse(
            export_path,
            media_type="text/csv",
            filename=f"{session_id}-closed-trade-diagnostics.csv",
        )
