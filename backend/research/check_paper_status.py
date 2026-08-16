#!/usr/bin/env python3
"""Check the current status of the paired paper trading sessions."""

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "paper_sessions"


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stdout + (f"\n[stderr: {result.stderr}]" if result.stderr else "")


def main() -> None:
    print("=== PM2 paper apps ===")
    pm2_out = run(["pm2", "list"])
    for line in pm2_out.splitlines():
        if "vibe-paper" in line:
            print(line)

    print("\n=== Session status ===")
    for regimen in ["5m", "10m", "15m"]:
        for label, subdir in [("control", f"shadow_ab_v2_control_{regimen}"),
                              ("candidate10", f"shadow_ab_v2_candidate10_{regimen}")]:
            session_dir = BASE / subdir
            if not session_dir.exists():
                print(f"{regimen}/{label}: MISSING")
                continue
            session = json.loads((session_dir / "session.json").read_text())
            book = json.loads((session_dir / "book.json").read_text())
            marks_path = session_dir / "marks.jsonl"
            mark_count = sum(1 for _ in marks_path.open()) if marks_path.exists() else 0
            last_mark = None
            if mark_count:
                last_mark = json.loads(marks_path.read_text().splitlines()[-1])
            print(
                f"{regimen}/{label}: marks={mark_count} cash={book['cash_remaining']:.6f} "
                f"status={session.get('accounting_status','?')} "
                f"last={last_mark['timestamp'] if last_mark else 'n/a'} "
                f"equity={last_mark['equity']:.4f} pnl_pct={last_mark['pnl_pct']:.4f}"
                if last_mark
                else f"{regimen}/{label}: marks={mark_count} cash={book['cash_remaining']:.6f} status={session.get('accounting_status','?')}"
            )

    print("\n=== 5m control latest mark prices ===")
    control_5m = BASE / "shadow_ab_v2_control_5m" / "marks.jsonl"
    if control_5m.exists():
        last = json.loads(control_5m.read_text().splitlines()[-1])
        print(f"timestamp: {last['timestamp']}")
        for sym, price in sorted(last.get("prices", {}).items()):
            print(f"  {sym}: {price}")

    print("\n=== 5m control pm2 logs (last 20) ===")
    print(run(["pm2", "logs", "vibe-paper-5m-paired", "--nostream", "--lines", "20"]))


if __name__ == "__main__":
    main()
