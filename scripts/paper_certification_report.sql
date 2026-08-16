\set ON_ERROR_STOP on
\pset pager off

\echo '### PAPER CERTIFICATION POLICY'
SELECT 150 AS minimum_total_closed_trades,
       false AS live_execution_enabled,
       'No automatic promotion; review per-worker net results after costs.' AS promotion_rule;

\echo '### PER-WORKER EVIDENCE'
WITH cycle_rollup AS (
  SELECT account_id,
         max(trades_closed) AS closed_trades,
         count(*) AS cycles,
         count(*) FILTER (
           WHERE status = 'COMPLETED'
             AND market_data_fresh
             AND ledger_reconciled
         ) AS clean_cycles,
         max(cycle_completed_at) AS latest_cycle
  FROM paper_trading.paper_cycle_events
  GROUP BY account_id
)
SELECT a.worker_id,
       a.strategy_id,
       a.timeframe,
       a.leverage,
       c.closed_trades,
       round((a.current_equity - a.initial_capital)::numeric, 8) AS net_pnl,
       round(a.realized_pnl::numeric, 8) AS realized_pnl,
       round(a.unrealized_pnl::numeric, 8) AS unrealized_pnl,
       round(a.fees::numeric, 8) AS fees,
       round(a.funding_pnl::numeric, 8) AS funding_pnl,
       a.ledger_status,
       c.clean_cycles,
       c.cycles,
       round((c.clean_cycles::numeric / nullif(c.cycles, 0)) * 100, 3) AS clean_cycle_pct,
       now() - c.latest_cycle AS cycle_age
FROM paper_trading.trading_accounts a
JOIN cycle_rollup c USING (account_id)
WHERE a.mode = 'paper'
ORDER BY a.worker_id;

\echo '### AGGREGATE SAMPLE GATE'
WITH per_worker AS (
  SELECT account_id, max(trades_closed) AS closed_trades
  FROM paper_trading.paper_cycle_events
  GROUP BY account_id
), totals AS (
  SELECT sum(p.closed_trades)::integer AS total_closed_trades,
         sum(a.current_equity - a.initial_capital) AS total_net_pnl,
         sum(a.fees) AS total_fees,
         sum(a.funding_pnl) AS total_funding_pnl,
         count(*) FILTER (WHERE a.current_equity > a.initial_capital) AS positive_net_workers,
         count(*) AS workers,
         bool_and(a.ledger_status = 'OK') AS all_ledgers_ok
  FROM paper_trading.trading_accounts a
  JOIN per_worker p USING (account_id)
  WHERE a.mode = 'paper'
)
SELECT *,
       total_closed_trades >= 150 AS sample_gate_met,
       false AS live_execution_unlocked
FROM totals;
