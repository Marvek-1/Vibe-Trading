#!/usr/bin/env bash
set -euo pipefail

BUNDLE=${1:-/root/incoming/vibe-final-20260810_1542}
COMPOSE_FILE=/opt/vibe-trading/backend/scripts/docker-compose.yml
CUTOVER_DB=idim_ikang_cutover
ROLLBACK_DB=idim_ikang_candidate_20260810_1542
AGENT_DIR=/opt/vibe-trading/backend/agent

cd "$BUNDLE"
sha256sum -c SHA256SUMS

cd /opt/vibe-trading
DB_CONTAINER=$(docker compose -f "$COMPOSE_FILE" ps -q paper-db)
test -n "$DB_CONTAINER"

database_exists() {
  docker exec "$DB_CONTAINER" psql -U postgres -d postgres -Atc \
    "SELECT count(*) FROM pg_database WHERE datname='$1';"
}

if test "$(database_exists "$CUTOVER_DB")" != "0"; then
  echo "Refusing to overwrite existing database: $CUTOVER_DB"
  exit 1
fi
if test "$(database_exists "$ROLLBACK_DB")" != "0"; then
  echo "Refusing to overwrite existing database: $ROLLBACK_DB"
  exit 1
fi

docker exec "$DB_CONTAINER" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
  "CREATE DATABASE $CUTOVER_DB WITH TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8';"

docker cp "$BUNDLE/idim_ikang_final.dump" "$DB_CONTAINER:/tmp/idim_ikang_final.dump"
RESTORE_STARTED=$(date +%s)
docker exec "$DB_CONTAINER" pg_restore \
  -U postgres -d "$CUTOVER_DB" --no-owner --no-privileges --exit-on-error \
  /tmp/idim_ikang_final.dump
RESTORE_FINISHED=$(date +%s)
docker exec --user root "$DB_CONTAINER" rm -f /tmp/idim_ikang_final.dump
echo "restore_seconds=$((RESTORE_FINISHED - RESTORE_STARTED))"

docker exec -i "$DB_CONTAINER" psql -U postgres -d "$CUTOVER_DB" \
  -v ON_ERROR_STOP=1 -f - < "$BUNDLE/cutover_manifest.sql" \
  > "$BUNDLE/manifest_vps_final.txt"

awk '/^### 01 /{keep=1} /^### 13 /{keep=0} keep{print}' \
  "$BUNDLE/manifest_dell.txt" > "$BUNDLE/manifest_dell.stable.txt"
awk '/^### 01 /{keep=1} /^### 13 /{keep=0} keep{print}' \
  "$BUNDLE/manifest_vps_final.txt" > "$BUNDLE/manifest_vps_final.stable.txt"
diff -u "$BUNDLE/manifest_dell.stable.txt" "$BUNDLE/manifest_vps_final.stable.txt" \
  > "$BUNDLE/manifest_final.diff"
test ! -s "$BUNDLE/manifest_final.diff"

docker exec "$DB_CONTAINER" vacuumdb -U postgres --analyze-in-stages -d "$CUTOVER_DB"

docker compose -f "$COMPOSE_FILE" stop -t 60 frontend vibe-trading || true
docker exec "$DB_CONTAINER" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('idim_ikang','$CUTOVER_DB') AND pid <> pg_backend_pid();"
docker exec "$DB_CONTAINER" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
  "ALTER DATABASE idim_ikang RENAME TO $ROLLBACK_DB; ALTER DATABASE $CUTOVER_DB RENAME TO idim_ikang;"

tar -C "$AGENT_DIR" -czf "$BUNDLE/paper_sessions_vps_before_final.tar.gz" \
  paper_sessions config/paper_sessions_registry.json
rm -rf "$AGENT_DIR/paper_sessions"
tar -C "$AGENT_DIR" -xzf "$BUNDLE/paper_sessions_final.tar.gz"

cp "$COMPOSE_FILE" "$BUNDLE/docker-compose.vps-before-final.yml"
install -o root -g root -m 0644 "$BUNDLE/docker-compose.yml" "$COMPOSE_FILE"

VIBE_UID=$(docker run --rm --entrypoint sh vibe-trading-main-paper-runtime:latest -c 'id -u vibe')
VIBE_GID=$(docker run --rm --entrypoint sh vibe-trading-main-paper-runtime:latest -c 'id -g vibe')
chown -R "$VIBE_UID:$VIBE_GID" "$AGENT_DIR/paper_sessions"
chown "$VIBE_UID:$VIBE_GID" "$AGENT_DIR/.env"
chmod 600 "$AGENT_DIR/.env"

docker compose -f "$COMPOSE_FILE" config --quiet
docker compose -f "$COMPOSE_FILE" up -d --force-recreate paper-db vibe-trading frontend

for attempt in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8899/openapi.json >/dev/null 2>&1; then
    break
  fi
  if test "$attempt" -eq 60; then
    docker compose -f "$COMPOSE_FILE" logs --tail=200 vibe-trading
    exit 1
  fi
  sleep 2
done

docker compose -f "$COMPOSE_FILE" ps
ss -ltnp | grep -E ':5899|:8899' || true
sha256sum "$BUNDLE/manifest_vps_final.txt" "$BUNDLE/manifest_vps_final.stable.txt"
echo "VPS_PROMOTED_READY_FOR_WORKERS"
