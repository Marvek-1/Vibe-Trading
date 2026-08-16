#!/usr/bin/env python3
"""Safely inspect and conditionally unfreeze paper-trading sessions.

Default mode is dry-run. The tool refuses to clear ACCOUNTING_ERROR unless:
1. session.json and book.json receipts verify;
2. every configured symbol has a finite, positive fresh price;
3. book quantities equal quantities reconstructed from trades; and
4. the fully-valued self-financing residual is within tolerance.

It never rewrites book.json or trades.jsonl. A failing session remains frozen
and must be reconstructed/quarantined with the repository reconciliation tool.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agent"
sys.path.insert(0, str(AGENT_DIR))

import paper_session as ps  # noqa: E402
from paper_accounting_guard import (  # noqa: E402
    assess_accounting,
    normalize_price_snapshot,
    position_ledger_differences,
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.pre-recovery.{_timestamp()}.bak")
    shutil.copy2(path, backup)
    hash_path = path.with_suffix(path.suffix + ".hash")
    if hash_path.exists():
        shutil.copy2(
            hash_path,
            backup.with_suffix(backup.suffix + ".hash"),
        )
    return backup


def inspect_session(session_dir: Path) -> dict[str, Any]:
    required = (session_dir / "session.json", session_dir / "book.json")
    for path in required:
        if not path.exists():
            raise FileNotFoundError(path)
        if not ps.verify_receipted_file(path):
            raise RuntimeError(f"receipt verification failed: {path}")

    session = ps._load_session(session_dir)
    book = ps._load_book(session_dir)
    trades = ps._read_jsonl(session_dir / "trades.jsonl")
    trade_stats = ps.compute_trade_stats(trades)

    prices = normalize_price_snapshot(
        session["symbols"],
        ps.fetch_last_prices(session["symbols"]),
    )
    candidate_mark = ps._build_mark(
        session,
        book,
        prices,
        now=datetime.now(timezone.utc).isoformat(),
    )
    position = ps._compute_unrealized_position_pnl(
        trade_stats["by_symbol"],
        candidate_mark["prices"],
    )
    differences = position_ledger_differences(
        book.get("positions", {}),
        trade_stats["by_symbol"],
    )
    decision = assess_accounting(
        configured_symbols=session["symbols"],
        initial_cash=float(session["initial_cash"]),
        equity=float(candidate_mark["equity"]),
        realized_pnl=float(trade_stats["overall"]["realized_pnl"]),
        unrealized_pnl=position["unrealized_pnl"],
        stale_mark_symbols=position.get("stale_mark_symbols", []),
        position_differences=differences,
        abs_tolerance=ps.RECONCILIATION_ABS_TOLERANCE,
        rel_tolerance=ps.RECONCILIATION_REL_TOLERANCE,
    )
    return {
        "session": session,
        "book": book,
        "candidate_mark": candidate_mark,
        "position": position,
        "decision": decision,
    }


def recover(session_dir: Path, *, apply: bool) -> dict[str, Any]:
    inspected = inspect_session(session_dir)
    session = inspected["session"]
    decision = inspected["decision"]

    report: dict[str, Any] = {
        "session_id": session_dir.name,
        "current_status": session.get("accounting_status"),
        "decision": decision.to_dict(),
        "action": "NO_CHANGE",
    }

    if decision.state != "OK":
        report["action"] = "REFUSED"
        return report

    if session.get("accounting_status") != "ACCOUNTING_ERROR":
        report["action"] = "ALREADY_OK"
        return report

    if not apply:
        report["action"] = "WOULD_UNFREEZE"
        return report

    backup = _backup(session_dir / "session.json")
    updated = dict(session)
    updated["accounting_status"] = "OK"
    for key in (
        "accounting_error",
        "accounting_error_detected_at",
        "accounting_error_kind",
        "accounting_position_differences",
        "accounting_stale_mark_symbols",
    ):
        updated.pop(key, None)
    updated["accounting_recovered_at"] = datetime.now(timezone.utc).isoformat()
    updated["accounting_recovery_method"] = "fresh-price-full-ledger-reconciliation"

    ps.receipted_write(
        session_dir / "session.json",
        json.dumps(updated, indent=2),
    )
    ps._mirror_session_to_store(session_dir.name, updated)
    ps._append_jsonl(
        session_dir / "recovery_audit.jsonl",
        {
            "timestamp": updated["accounting_recovered_at"],
            "event": "accounting_status_recovered",
            "previous_status": "ACCOUNTING_ERROR",
            "new_status": "OK",
            "decision": decision.to_dict(),
            "backup": backup.name,
        },
    )
    report["action"] = "UNFROZEN"
    report["backup"] = str(backup)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--session-root",
        type=Path,
        default=REPO_ROOT / "agent" / "paper_sessions",
    )
    parser.add_argument("--session-id", action="append", default=[])
    parser.add_argument(
        "--all-errors",
        action="store_true",
        help="inspect every session currently marked ACCOUNTING_ERROR",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform eligible status changes; default is dry-run",
    )
    args = parser.parse_args()

    session_dirs: list[Path] = []
    if args.session_id:
        session_dirs.extend(args.session_root / value for value in args.session_id)

    if args.all_errors:
        for candidate in sorted(args.session_root.iterdir()):
            session_path = candidate / "session.json"
            if not session_path.exists():
                continue
            try:
                payload = json.loads(session_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("accounting_status") == "ACCOUNTING_ERROR":
                session_dirs.append(candidate)

    unique_dirs = list(dict.fromkeys(session_dirs))
    if not unique_dirs:
        parser.error("provide --session-id or --all-errors")

    exit_code = 0
    for session_dir in unique_dirs:
        try:
            report = recover(session_dir, apply=args.apply)
        except Exception as exc:  # noqa: BLE001
            report = {
                "session_id": session_dir.name,
                "action": "ERROR",
                "error": str(exc),
            }
            exit_code = 1
        print(json.dumps(report, sort_keys=True))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
