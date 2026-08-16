#!/usr/bin/env bash
set -euo pipefail

LOCAL_ROOT=/root/backups/vibe-daily
REMOTE_ROOT=/home/idona/backups/vibe-daily
REMOTE_HOST=idona@10.10.0.2
SSH_KEY=/root/.ssh/vibe_migration
DB_CONTAINER=vibe-trading-main-paper-db-1
DB_NAME=idim_ikang
STAMP=$(date -u +%Y%m%d_%H%M%S)
PARTIAL="$LOCAL_ROOT/.partial-$STAMP"
FINAL="$LOCAL_ROOT/$STAMP"

case "$LOCAL_ROOT" in
  /root/backups/vibe-daily) ;;
  *) echo "unsafe LOCAL_ROOT: $LOCAL_ROOT" >&2; exit 1 ;;
esac
case "$REMOTE_ROOT" in
  /home/idona/backups/vibe-daily) ;;
  *) echo "unsafe REMOTE_ROOT: $REMOTE_ROOT" >&2; exit 1 ;;
esac

exec 9>/run/lock/vibe-daily-backup.lock
flock -n 9 || { echo "another Vibe backup is already running"; exit 0; }

install -d -o root -g root -m 700 "$LOCAL_ROOT" "$PARTIAL"
cleanup() { rm -rf -- "$PARTIAL"; }
trap cleanup EXIT

docker inspect "$DB_CONTAINER" >/dev/null
docker exec "$DB_CONTAINER" pg_isready -U postgres -d "$DB_NAME" >/dev/null

STARTED_AT=$(date -u +%FT%TZ)
START_SECONDS=$(date +%s)
docker exec "$DB_CONTAINER" \
  pg_dump -U postgres -d "$DB_NAME" -Fc -Z6 \
  > "$PARTIAL/$DB_NAME.dump"
pg_restore -l "$PARTIAL/$DB_NAME.dump" > "$PARTIAL/restore-list.txt"
docker exec "$DB_CONTAINER" psql -U postgres -d "$DB_NAME" -Atc \
  "select count(*), max(created_at) from paper_trading.paper_cycle_events;" \
  > "$PARTIAL/cycle-receipt.txt"
ENDED_AT=$(date -u +%FT%TZ)
END_SECONDS=$(date +%s)
printf 'started_at=%s\nended_at=%s\nduration_seconds=%s\n' \
  "$STARTED_AT" "$ENDED_AT" "$((END_SECONDS-START_SECONDS))" \
  > "$PARTIAL/metadata.txt"

(
  cd "$PARTIAL"
  sha256sum "$DB_NAME.dump" restore-list.txt cycle-receipt.txt metadata.txt \
    > SHA256SUMS
  sha256sum -c SHA256SUMS
)
chmod 600 "$PARTIAL"/*
mv "$PARTIAL" "$FINAL"
trap - EXIT

ssh -o BatchMode=yes -o ConnectTimeout=15 -i "$SSH_KEY" "$REMOTE_HOST" \
  "install -d -m 700 '$REMOTE_ROOT' '$REMOTE_ROOT/.partial-$STAMP'"
scp -q -i "$SSH_KEY" "$FINAL"/* \
  "$REMOTE_HOST:$REMOTE_ROOT/.partial-$STAMP/"
ssh -o BatchMode=yes -o ConnectTimeout=15 -i "$SSH_KEY" "$REMOTE_HOST" \
  "set -e; cd '$REMOTE_ROOT/.partial-$STAMP'; sha256sum -c SHA256SUMS; pg_restore -l '$DB_NAME.dump' >/dev/null; mv '$REMOTE_ROOT/.partial-$STAMP' '$REMOTE_ROOT/$STAMP'"

# Retain fourteen completed daily copies on each failure domain. Partial
# directories are deliberately excluded and remain visible for diagnosis.
mapfile -t LOCAL_OLD < <(
  find "$LOCAL_ROOT" -mindepth 1 -maxdepth 1 -type d \
    -name '[0-9]*_[0-9]*' -printf '%T@ %p\n' | sort -rn | tail -n +15 | cut -d' ' -f2-
)
for path in "${LOCAL_OLD[@]}"; do
  case "$path" in "$LOCAL_ROOT"/[0-9]*_[0-9]*) rm -rf -- "$path" ;; esac
done

ssh -o BatchMode=yes -i "$SSH_KEY" "$REMOTE_HOST" \
  "ROOT='$REMOTE_ROOT'; mapfile -t old < <(find \"\$ROOT\" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*_[0-9]*' -printf '%T@ %p\\n' | sort -rn | tail -n +15 | cut -d' ' -f2-); for path in \"\${old[@]}\"; do case \"\$path\" in \"\$ROOT\"/[0-9]*_[0-9]*) rm -rf -- \"\$path\";; esac; done"

echo "VIBE_BACKUP_ACCEPTED local=$FINAL remote=$REMOTE_HOST:$REMOTE_ROOT/$STAMP"
