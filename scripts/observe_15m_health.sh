#!/usr/bin/env bash
set -euo pipefail

DB_CONTAINER="${DB_CONTAINER:-vibe-trading-main-paper-db-1}"
CONTROL_CONTAINER="${CONTROL_CONTAINER:-vibe-trading-main-control-15m-1}"
CANDIDATE_CONTAINER="${CANDIDATE_CONTAINER:-vibe-trading-main-candidate-15m-1}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-2400}"
POLL_SECONDS="${POLL_SECONDS:-30}"

cycle_count() {
  local worker="$1"
  docker exec "$DB_CONTAINER" psql -U postgres -d idim_ikang -Atc \
    "SELECT count(*) FROM paper_trading.paper_cycle_events WHERE worker_id='${worker}';"
}

health() {
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}NONE{{end}}' "$1"
}

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
start_epoch="$(date +%s)"
control_base="$(cycle_count control_15m)"
candidate_base="$(cycle_count candidate_15m)"

printf 'started_at=%s control_base=%s candidate_base=%s\n' \
  "$started_at" "$control_base" "$candidate_base"

while true; do
  control_now="$(cycle_count control_15m)"
  candidate_now="$(cycle_count candidate_15m)"
  control_health="$(health "$CONTROL_CONTAINER")"
  candidate_health="$(health "$CANDIDATE_CONTAINER")"

  printf '%s control=%s(+%s) health=%s candidate=%s(+%s) health=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$control_now" "$((control_now - control_base))" "$control_health" \
    "$candidate_now" "$((candidate_now - candidate_base))" "$candidate_health"

  if [[ "$control_health" != healthy || "$candidate_health" != healthy ]]; then
    echo 'FRESHNESS_OBSERVATION_FAILED health'
    exit 1
  fi

  if (( control_now >= control_base + 2 && candidate_now >= candidate_base + 2 )); then
    echo 'FRESHNESS_OBSERVATION_ACCEPTED'
    exit 0
  fi

  if (( $(date +%s) - start_epoch >= TIMEOUT_SECONDS )); then
    echo 'FRESHNESS_OBSERVATION_FAILED timeout'
    exit 1
  fi

  sleep "$POLL_SECONDS"
done
