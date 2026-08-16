#!/usr/bin/env python3
"""Production reconciliation gate for natural Grid multi-leg positions.

Exit 0 only when a 2+ same-symbol leg state has been observed and the
authoritative engine snapshot agrees exactly with PostgreSQL. Exit 1 means
either a mismatch or that the production condition has not occurred yet.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


DB_QUERY = """
SELECT
    a.worker_id,
    p.symbol,
    count(*) AS row_count,
    count(DISTINCT p.trade_id) AS distinct_trade_ids
FROM paper_trading.positions p
JOIN paper_trading.trading_accounts a USING (account_id)
WHERE a.worker_id = %(worker_id)s
GROUP BY a.worker_id, p.symbol;
"""


def engine_counts_by_symbol(account_json_path: Path) -> Counter[str]:
    state = json.loads(account_json_path.read_text(encoding="utf-8"))
    positions = state.get("positions", {})
    if not isinstance(positions, dict):
        raise ValueError(f"{account_json_path}: positions must be an object")
    counter: Counter[str] = Counter()
    for position in positions.values():
        counter[str(position["symbol"])] += 1
    return counter


def db_counts_by_symbol(conn, worker_id: str) -> dict[str, dict[str, int]]:
    with conn.cursor() as cursor:
        cursor.execute(DB_QUERY, {"worker_id": worker_id})
        rows = cursor.fetchall()
    result: dict[str, dict[str, int]] = {}
    for _worker_id, symbol, row_count, distinct_trade_ids in rows:
        row_count = int(row_count)
        distinct_trade_ids = int(distinct_trade_ids)
        if row_count != distinct_trade_ids:
            raise RuntimeError(
                f"PRIMARY KEY INTEGRITY FAILURE for {worker_id}/{symbol}: "
                f"row_count={row_count} != distinct_trade_ids={distinct_trade_ids}. "
                "Stop and investigate before proceeding."
            )
        result[str(symbol)] = {
            "row_count": row_count,
            "distinct_trade_ids": distinct_trade_ids,
        }
    return result


def run_gate(conn, worker_id: str, account_json_path: Path) -> dict[str, Any]:
    engine_counts = engine_counts_by_symbol(account_json_path)
    db_counts = db_counts_by_symbol(conn, worker_id)
    rows: list[dict[str, Any]] = []
    any_mismatch = False
    multileg_observed = False

    for symbol in sorted(set(engine_counts) | set(db_counts)):
        engine_count = engine_counts.get(symbol, 0)
        db_count = db_counts.get(symbol, {}).get("row_count", 0)
        matches = engine_count == db_count
        any_mismatch |= not matches
        multileg_observed |= engine_count > 1 or db_count > 1
        rows.append(
            {
                "symbol": symbol,
                "engine_open_legs": engine_count,
                "db_row_count": db_count,
                "match": matches,
            }
        )

    return {
        "worker_id": worker_id,
        "rows": rows,
        "any_mismatch": any_mismatch,
        "multileg_observed": multileg_observed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument(
        "--worker", action="append", required=True,
        metavar="WORKER_ID=ACCOUNT_JSON_PATH",
    )
    args = parser.parse_args()

    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed", file=sys.stderr)
        return 1

    overall_pass = True
    any_multileg = False
    with psycopg.connect(args.dsn) as connection:
        for spec in args.worker:
            worker_id, separator, path_string = spec.partition("=")
            if not separator or not worker_id or not path_string:
                parser.error(f"invalid --worker value: {spec!r}")
            result = run_gate(connection, worker_id, Path(path_string))
            print(f"\n=== {worker_id} ===")
            if not result["rows"]:
                print("  no open positions in engine or PostgreSQL")
            for row in result["rows"]:
                status = "OK" if row["match"] else "MISMATCH"
                note = " (multi-leg)" if max(row["engine_open_legs"], row["db_row_count"]) > 1 else ""
                print(
                    f"  {row['symbol']:<12} engine={row['engine_open_legs']} "
                    f"db={row['db_row_count']} [{status}]{note}"
                )
            overall_pass &= not result["any_mismatch"]
            any_multileg |= result["multileg_observed"]

    if not overall_pass:
        print("\nGATE: FAILED -- engine/DB counts diverge. Keep positions_legacy.")
        return 1
    if not any_multileg:
        print("\nGATE: NOT YET EXERCISED -- no 2+ same-symbol state observed. Keep positions_legacy.")
        return 1
    print("\nGATE: PASSED -- multi-leg state observed and engine/DB counts match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
