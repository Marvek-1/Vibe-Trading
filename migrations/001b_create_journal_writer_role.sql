-- 001b_create_journal_writer_role.sql
-- Split out from 001_journal_events.sql so this file, and only this file,
-- can be run with statement echo OFF -- it's the only statement in the
-- whole migration that carries a live secret (the role's password), and it
-- must never be printed to a log, terminal transcript, or PR receipt.
--
-- Run via run_001_journal_events.sh, which invokes this quietly (no -a) and
-- reports only success/failure, never the statement text.
SELECT 'CREATE ROLE journal_writer LOGIN PASSWORD ' || quote_literal(:'journal_writer_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'journal_writer')
\gexec
