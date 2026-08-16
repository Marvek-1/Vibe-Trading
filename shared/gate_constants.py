"""Statistical gate constants for the append-only trade journal.

Single source of truth for the numbers that decide whether a symbol pocket's
track record is large enough to trust. Nothing computes ``gate_eligible`` and
stores it — that field is never persisted (see journal_stats in
tradingview-mcp/storage/base.py). It is derived at read time from these
constants so a stale stored flag can never outlive the data that justified it.

Import these, don't retype them:
    from shared.gate_constants import GATE_MIN_RESOLVED, GATE_MIN_PF, KILL_CRITERION_AT, INTEGRATION_EPOCH
"""

from __future__ import annotations

from datetime import datetime, timezone

# A symbol pocket needs at least this many resolved (ENTRY+EXIT paired) trades
# before profit factor means anything. Below this, PF is a coin flip wearing
# decimals -- see the claim-discipline rule in the builder brief.
GATE_MIN_RESOLVED = 200

# ... and even above that count, PF must clear this to be gate-eligible.
GATE_MIN_PF = 1.30

# Below GATE_MIN_RESOLVED, a *separate*, weaker bar exists for an early kill
# signal: enough resolved trades to catch a clearly-losing pocket before it
# ever reaches the full gate, without waiting 200 trades to say "stop".
KILL_CRITERION_AT = 150

# UTC instant this journal integration went live. Every stats query filters
# ts_utc >= INTEGRATION_EPOCH -- the tainted 2026-07-05 through 2026-07-11
# window (see agent/src/idimikang/store.py's own _TAINT_START/_TAINT_END) is
# structurally excluded from gate accounting, not just flagged. No legacy
# credit: gate accounting starts at zero from this instant, permanently.
INTEGRATION_EPOCH = datetime(2026, 7, 12, 6, 0, 0, tzinfo=timezone.utc).timestamp()
