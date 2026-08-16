#!/usr/bin/env bash
set -euo pipefail

DB_CONTAINER=vibe-trading-main-paper-db-1
DB_NAME=idim_ikang
SECRET_DIR=/opt/vibe-trading/secrets
NETWORK=vibe-trading-main_default
IMAGE=vibe-trading-main-paper-runtime

install -d -o root -g root -m 700 "$SECRET_DIR"
WORKER_PASSWORD=$(openssl rand -hex 32)
APP_PASSWORD=$(openssl rand -hex 32)
DEV_PASSWORD=$(openssl rand -hex 32)

docker exec -i "$DB_CONTAINER" psql -U postgres -d "$DB_NAME" \
  -v ON_ERROR_STOP=1 >/dev/null <<SQL
SET password_encryption = 'scram-sha-256';
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vibe_worker') THEN
    CREATE ROLE vibe_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vibe_app') THEN
    CREATE ROLE vibe_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vibe_dev') THEN
    CREATE ROLE vibe_dev LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END
\$\$;
ALTER ROLE vibe_worker PASSWORD '$WORKER_PASSWORD';
ALTER ROLE vibe_app PASSWORD '$APP_PASSWORD';
ALTER ROLE vibe_dev PASSWORD '$DEV_PASSWORD';

GRANT CONNECT ON DATABASE idim_ikang TO vibe_worker, vibe_app, vibe_dev;
GRANT USAGE ON SCHEMA paper_trading TO vibe_worker, vibe_app, vibe_dev;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA paper_trading TO vibe_worker, vibe_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA paper_trading TO vibe_worker, vibe_app;
GRANT SELECT ON ALL TABLES IN SCHEMA paper_trading TO vibe_dev;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA paper_trading TO vibe_dev;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA paper_trading
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vibe_worker, vibe_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA paper_trading
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO vibe_worker, vibe_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA paper_trading
  GRANT SELECT ON TABLES TO vibe_dev;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA paper_trading
  GRANT SELECT ON SEQUENCES TO vibe_dev;
SQL

printf 'paper-db:5432:%s:vibe_worker:%s\n' "$DB_NAME" "$WORKER_PASSWORD" \
  > "$SECRET_DIR/vibe_worker.pgpass"
printf 'paper-db:5432:%s:vibe_app:%s\n' "$DB_NAME" "$APP_PASSWORD" \
  > "$SECRET_DIR/vibe_app.pgpass"
printf 'paper-db:5432:%s:vibe_dev:%s\n' "$DB_NAME" "$DEV_PASSWORD" \
  > "$SECRET_DIR/vibe_dev.pgpass"
chown 1000:1000 "$SECRET_DIR"/*.pgpass
chmod 600 "$SECRET_DIR"/*.pgpass
unset WORKER_PASSWORD APP_PASSWORD DEV_PASSWORD

docker exec "$DB_CONTAINER" sh -eu -c '
  HBA=/var/lib/postgresql/data/pg_hba.conf
  cp -p "$HBA" "$HBA.pre-scoped-roles"
  if ! grep -q "^host all vibe_worker all scram-sha-256$" "$HBA"; then
    sed -i "/^host all all all trust$/i\\
host all vibe_worker all scram-sha-256\\
host all vibe_app all scram-sha-256\\
host all vibe_dev all scram-sha-256" "$HBA"
  fi
'
docker exec "$DB_CONTAINER" psql -U postgres -d postgres -Atc \
  "select pg_reload_conf();" >/dev/null

for role in vibe_worker vibe_app; do
  docker run --rm --network "$NETWORK" \
    -e "VIBE_TEST_ROLE=$role" \
    -e "PGPASSFILE=/run/secrets/$role.pgpass" \
    -v "$SECRET_DIR/$role.pgpass:/run/secrets/$role.pgpass:ro" \
    "$IMAGE" python -c \
    'import os, psycopg; role=os.environ["VIBE_TEST_ROLE"]; conn=psycopg.connect(f"postgresql://{role}@paper-db:5432/idim_ikang"); print(conn.execute("select current_user").fetchone()[0]); conn.close()'
done

echo SCOPED_POSTGRES_ROLES_PREPARED
