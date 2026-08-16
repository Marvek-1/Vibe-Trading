#!/usr/bin/env bash
# Auto-restarting wrapper for the Vite dev server.
#
# WSL2's virtual network layer has been observed dropping and resetting
# intermittently on this machine (dmesg: "WSL ... CheckConnection:
# connect()/getaddrinfo() failed"), which can kill a listening dev-server
# process outright. This wraps vite in a restart loop so a network blip
# means a few seconds of downtime instead of a dead port until someone
# notices and restarts it by hand.
set -u
cd "$(dirname "$0")"
LOG="/tmp/vibe_frontend_5899.log"
while true; do
  echo "[supervisor] starting vite at $(date -u -Iseconds)" >> "$LOG"
  npx vite --host 127.0.0.1 --port 5899 >> "$LOG" 2>&1
  echo "[supervisor] vite exited (code $?) at $(date -u -Iseconds), restarting in 2s" >> "$LOG"
  sleep 2
done
