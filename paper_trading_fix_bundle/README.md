# Paper Trading Hardening Bundle

## Verified diagnosis

The proposed UI cutover is sound: route `/paper-trading` to the component that
uses `/paper-sessions`, removing the dead Docker loopback proxy from the active
path.

The original backend root-cause claim is not supported by the supplied source:

- `_build_mark()` uses direct `prices[code]` access.
- `rebalance_if_due()` also uses direct `prices[code]` access.
- `fetch_last_prices()` raises when a ticker has no `last`.

A genuinely missing key therefore raises; it is not silently valued at zero.
The exact `$2,000` residual is more consistent with legacy state divergence
between `book.json`, `trades.jsonl`, and/or the configured symbol universe.

The current invariant does contain a real bug: when
`_compute_unrealized_position_pnl()` returns `None` because an open trade symbol
has no mark, the code converts “indeterminate” directly into
`ACCOUNTING_ERROR`. This bundle separates:

- `DEFERRED`: incomplete valuation evidence; do not freeze.
- `ERROR`: complete numeric residual, ledger quantity mismatch, or symbol-set
  mismatch; freeze.
- `OK`: fully reconciled.

## Files

- `agent/paper_accounting_guard.py`: typed, dependency-free guard logic.
- `paper_trading_hardening.patch`: integration patch for confirmed source
  contexts plus the router cutover.
- `tools/recover_paper_sessions.py`: dry-run-first recovery utility.
- `tests/test_paper_accounting_guard.py`: unit tests.
- `TEST_REPORT.txt`: executed test output.

## Apply

1. Copy `agent/paper_accounting_guard.py` into the repository.
2. Copy `tools/recover_paper_sessions.py` into the repository.
3. Apply the `paper_session.py` and `router.tsx` hunks from
   `paper_trading_hardening.patch`.
4. Add the test file to the repository test suite.
5. Run:

```bash
python -m unittest -v tests.test_paper_accounting_guard
npm run typecheck
npm run build
```

The frontend build commands were not run here because the actual frontend
repository and package manifest were not present in the execution workspace.

## Recover frozen sessions

Dry run all frozen sessions:

```bash
python tools/recover_paper_sessions.py --all-errors
```

Apply only after reviewing the JSON reports:

```bash
python tools/recover_paper_sessions.py --all-errors --apply
```

The utility refuses recovery when receipts fail, prices are incomplete, book
positions differ from trade-derived quantities, or the self-financing residual
is outside tolerance. It backs up `session.json` and rewrites it through
`receipted_write`; direct manual JSON edits are intentionally avoided.

## Rollout

- Stop all paper runners.
- Back up `agent/paper_sessions`.
- Deploy backend guard and tests.
- Run recovery in dry-run mode.
- Switch the frontend route.
- Build the frontend.
- Start one canary session.
- Monitor `accounting_check_deferred`, `accounting_error`, receipt failures,
  and poll exceptions.
- Roll back the route and backend commit if any healthy session develops a
  new residual or quantity mismatch.
