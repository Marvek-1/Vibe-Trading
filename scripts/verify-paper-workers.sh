#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sessions_root="$repo_root/backend/agent/paper_sessions"
max_heartbeat_age="${MAX_HEARTBEAT_AGE_SECONDS:-180}"
failures=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1" >&2; failures=$((failures + 1)); }

declare -a intervals=(5m 10m 15m)
declare -A seen_paths=()

for interval in "${intervals[@]}"; do
  control="shadow_a_vx_control_${interval}_okx"
  candidate="shadow_b_vx_candidate10_${interval}_okx"
  signature="scripts/shadow_ab_pair.py --control-dir paper_sessions/$control --candidate-dir paper_sessions/$candidate"

  process_count="$(pgrep -af -- "$signature" | wc -l)"
  if [[ "$process_count" -eq 1 ]]; then
    pass "$interval worker process is unique"
  else
    fail "$interval worker process count is $process_count (expected 1)"
  fi

  for role_and_name in "control:$control" "candidate:$candidate"; do
    role="${role_and_name%%:*}"
    name="${role_and_name#*:}"
    session_dir="$sessions_root/$name"
    heartbeat="$session_dir/.heartbeat"
    session_file="$session_dir/session.json"

    if [[ ! -d "$session_dir" ]]; then
      fail "$interval $role session directory is missing"
      continue
    fi

    resolved="$(realpath "$session_dir")"
    if [[ -n "${seen_paths[$resolved]:-}" ]]; then
      fail "$interval $role shares writable state with ${seen_paths[$resolved]}"
    else
      seen_paths[$resolved]="$interval $role"
      pass "$interval $role has isolated writable state"
    fi

    if [[ ! -f "$heartbeat" ]]; then
      fail "$interval $role heartbeat is missing"
    else
      age=$(( $(date +%s) - $(stat -c %Y "$heartbeat") ))
      if (( age >= 0 && age <= max_heartbeat_age )); then
        pass "$interval $role heartbeat age ${age}s"
      else
        fail "$interval $role heartbeat age ${age}s exceeds ${max_heartbeat_age}s"
      fi
    fi

    if [[ ! -f "$session_file" ]]; then
      fail "$interval $role session metadata is missing"
      continue
    fi

    accounting_status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("accounting_status", "MISSING"))' "$session_file" 2>/dev/null || printf 'INVALID')"
    if [[ "$accounting_status" == "OK" ]]; then
      pass "$interval $role accounting status is OK"
    else
      fail "$interval $role accounting status is $accounting_status"
    fi
  done
done

if (( failures > 0 )); then
  printf '\nPaper-worker gate failed with %d issue(s).\n' "$failures" >&2
  exit 1
fi

printf '\nPaper-worker gate passed.\n'
