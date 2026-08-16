#!/usr/bin/env bash
# Loud migration runner for 001_journal_events.sql.
#
# set -euo pipefail: any failing statement kills the whole run immediately,
# nonzero exit. All schema/table/index/grant DDL is echoed before it runs
# (psql -a) -- that output is the Witness receipt, paste it verbatim.
#
# The ONE statement that carries a live secret (CREATE ROLE ... PASSWORD) is
# split into 001b_create_journal_writer_role.sql and run in a SEPARATE,
# QUIET invocation (no -a, no echo) so the password literal is never printed
# to this script's output -- only a success/failure line is. Never merge
# 001b's contents back into a loudly-echoed run.
#
# DSN comes from TV_MCP_DSN so credentials are never typed on the command
# line or committed anywhere.
set -euo pipefail

: "${TV_MCP_DSN:?TV_MCP_DSN must be set (see tradingview-mcp/README.md)}"
: "${JOURNAL_WRITER_PASSWORD:?JOURNAL_WRITER_PASSWORD must be set (used only for CREATE ROLE, never stored)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Step 1/2: creating journal_writer role (quiet -- password never echoed) ==="
if psql "$TV_MCP_DSN" -v ON_ERROR_STOP=1 -q -o /dev/null \
    -v journal_writer_password="$JOURNAL_WRITER_PASSWORD" \
    -f "$SCRIPT_DIR/001b_create_journal_writer_role.sql"; then
    echo "journal_writer role: OK (created, or already existed)"
else
    echo "journal_writer role creation FAILED -- likely missing CREATEROLE privilege." >&2
    echo "Ask whoever holds the postgres superuser role to run 001b themselves, then re-run this script." >&2
    exit 1
fi

echo "=== Step 2/2: schema, tables, indexes, grants (loud -- this is the Witness receipt) ==="
psql "$TV_MCP_DSN" -v ON_ERROR_STOP=1 -a -f "$SCRIPT_DIR/001_journal_events.sql"

echo "=== Migration completed successfully ==="
