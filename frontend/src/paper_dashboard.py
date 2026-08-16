#!/usr/bin/env python3
"""MoStar Paper Trader — exact dashboard implementation.

This is a paper-only dashboard. It reads and mutates only the local simulated
futures ledger. It never sends real orders and requires no exchange credentials.

Run:
    python paper_dashboard.py \
      --session paper_sessions/demo \
      --initial-balance 10000 \
      --host 127.0.0.1 \
      --port 8787
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from futures_paper_engine import FuturesPaperEngine, fetch_mark_price, read_jsonl

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class PaperDashboard:
    def __init__(self, session_dir: Path, initial_balance: float) -> None:
        self.engine = FuturesPaperEngine(session_dir, initial_balance=initial_balance)
        self.session_dir = session_dir
        self.started_at = datetime.now(timezone.utc)
        self.lock = threading.RLock()
        self.last_error: str | None = None
        self.last_refresh_at: str | None = None

    def _marks(self, limit: int = 300) -> list[dict[str, Any]]:
        rows = read_jsonl(self.engine.marks_path)
        return rows[-max(1, min(limit, 2000)):]

    def _closed_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.engine.closed_trades()
        return list(reversed(rows[-max(1, min(limit, 2000)):]))

    @staticmethod
    def _trade_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        pnls = [as_float(row.get("net_pnl")) for row in rows]
        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x < 0]
        total = len(rows)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        largest_win = max(wins) if wins else None
        largest_loss = min(losses) if losses else None
        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": (len(wins) / total * 100.0) if total else None,
            "profit_factor": (gross_win / gross_loss) if gross_loss else None,
            "net_pnl": sum(pnls),
            "average_win": (gross_win / len(wins)) if wins else None,
            "average_loss": (sum(losses) / len(losses)) if losses else None,
            "largest_win": largest_win,
            "largest_loss": largest_loss,
        }

    @staticmethod
    def _health_score(account: dict[str, Any]) -> dict[str, Any]:
        wallet = max(as_float(account.get("wallet_balance")), 1e-9)
        reserved = max(as_float(account.get("reserved_margin")), 0.0)
        equity = max(as_float(account.get("current_equity")), 0.0)
        margin_usage = reserved / wallet * 100.0
        equity_loss = max(0.0, (wallet - equity) / wallet * 100.0)

        penalty = min(55.0, margin_usage * 0.65) + min(35.0, equity_loss * 1.8)
        score = max(0.0, min(100.0, 100.0 - penalty))
        if score >= 85:
            level = "Low"
            label = "Healthy"
        elif score >= 65:
            level = "Moderate"
            label = "Watch"
        else:
            level = "High"
            label = "At Risk"
        return {
            "score": score,
            "label": label,
            "risk_level": level,
            "margin_usage_pct": margin_usage,
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            try:
                account = self.engine.account_summary()
                self.last_refresh_at = utc_now()
                self.last_error = None
            except Exception as exc:  # preserve dashboard even if price refresh fails
                self.last_error = str(exc)
                account = self.engine._serialize_state()
                account.update({
                    "timestamp": utc_now(),
                    "open_positions": [],
                    "open_notional": 0.0,
                    "unrealized_pnl": 0.0,
                    "current_equity": as_float(account.get("wallet_balance")),
                    "pnl": as_float(account.get("wallet_balance")) - as_float(account.get("initial_balance")),
                    "pnl_pct": 0.0,
                    "fees_paid": as_float(account.get("total_fees")),
                    "funding_paid": as_float(account.get("total_funding")),
                    "realized_net_pnl": as_float(account.get("realized_net_pnl")),
                })

            trades = self._closed_trades()
            marks = self._marks()
            stats = self._trade_stats(list(reversed(trades)))
            health = self._health_score(account)
            uptime = int((datetime.now(timezone.utc) - self.started_at).total_seconds())

            return {
                "paper_only": True,
                "session_id": self.session_dir.name,
                "engine_status": "connected" if self.last_error is None else "degraded",
                "data_source": "Binance (Mark Price)",
                "last_error": self.last_error,
                "last_refresh_at": self.last_refresh_at,
                "uptime_seconds": uptime,
                "risk_settings": {
                    "leverage_allowed": "5x / 10x",
                    "margin_range": "$20 - $100",
                    "default_leverage": "5x",
                    "default_margin": "$50",
                    "margin_mode": "Isolated",
                },
                "account": account,
                "health": health,
                "stats": stats,
                "recent_trades": trades[:10],
                "equity_curve": [
                    {
                        "timestamp": row.get("timestamp"),
                        "equity": as_float(row.get("current_equity", row.get("equity"))),
                    }
                    for row in marks
                ],
            }

    def close_position(self, trade_id: str) -> dict[str, Any]:
        with self.lock:
            position = self.engine.state.positions.get(trade_id)
            if position is None:
                raise KeyError(f"Unknown open trade_id: {trade_id}")
            mark = fetch_mark_price(position.symbol)
            trade = self.engine.close_position(
                trade_id,
                price=mark,
                exit_reason="manual_dashboard",
                order_type="taker",
            )
            return asdict(trade)

    def close_all(self) -> list[dict[str, Any]]:
        results = []
        with self.lock:
            for trade_id in list(self.engine.state.positions):
                results.append(self.close_position(trade_id))
        return results


def make_handler(app: PaperDashboard):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MoStarPaperTrader/2.0"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, to_json_bytes(payload), "application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            size = int(self.headers.get("Content-Length", "0") or 0)
            if size <= 0:
                return {}
            return json.loads(self.rfile.read(size).decode("utf-8"))

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/status":
                self._json(HTTPStatus.OK, app.snapshot())
                return
            if parsed.path == "/api/trades":
                limit = int(parse_qs(parsed.query).get("limit", ["100"])[0])
                self._json(HTTPStatus.OK, app._closed_trades(limit))
                return
            if parsed.path == "/api/marks":
                limit = int(parse_qs(parsed.query).get("limit", ["300"])[0])
                self._json(HTTPStatus.OK, app._marks(limit))
                return
            if parsed.path == "/health":
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "paper_only": True,
                    "session_id": app.session_dir.name,
                    "timestamp": utc_now(),
                })
                return

            requested = "index.html" if parsed.path in ("", "/") else parsed.path.lstrip("/")
            target = (WEB / requested).resolve()
            try:
                if WEB.resolve() not in target.parents and target != WEB.resolve():
                    raise FileNotFoundError
                if not target.is_file():
                    raise FileNotFoundError
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
                    mime += "; charset=utf-8"
                self._send(HTTPStatus.OK, target.read_bytes(), mime)
            except FileNotFoundError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            try:
                route = urlparse(self.path).path
                payload = self._body()
                if route == "/api/positions/close":
                    trade_id = str(payload.get("trade_id", "")).strip()
                    if not trade_id:
                        raise ValueError("trade_id is required")
                    self._json(HTTPStatus.OK, {
                        "ok": True,
                        "closed_trade": app.close_position(trade_id),
                    })
                    return
                if route == "/api/session/close-all":
                    self._json(HTTPStatus.OK, {
                        "ok": True,
                        "closed_trades": app.close_all(),
                    })
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="MoStar Futures Paper Trading dashboard")
    parser.add_argument("--session", default="paper_sessions/demo")
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    app = PaperDashboard(Path(args.session), args.initial_balance)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(json.dumps({
        "event": "paper_dashboard_started",
        "url": f"http://{args.host}:{args.port}",
        "session": str(Path(args.session).resolve()),
        "paper_only": True,
    }, indent=2))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
