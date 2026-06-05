#!/usr/bin/env bash
# ============================================================================
# Bootstraps a local Postgres for Bedrock Ops Lens dev.
# Idempotent: safe to re-run. Uses the brew postgresql@15 service.
# ============================================================================
set -euo pipefail

DB_NAME="${DB_NAME:-bedrock_lens}"
DB_USER="${DB_USER:-bedrock_lens}"
DB_PASS="${DB_PASS:-bedrock_lens_dev}"
PG_PORT="${PG_PORT:-5432}"

# brew on Apple Silicon installs to /opt/homebrew, on Intel to /usr/local.
PG_BIN="$(brew --prefix postgresql@15 2>/dev/null)/bin"
if [[ ! -x "$PG_BIN/psql" ]]; then
    echo "ERROR: psql not found at $PG_BIN/psql"
    echo "Install with: brew install postgresql@15"
    exit 1
fi
export PATH="$PG_BIN:$PATH"

echo "[1/4] starting postgresql@15 service..."
brew services start postgresql@15 >/dev/null 2>&1 || true

# Wait for the socket to be live.
for i in {1..30}; do
    if pg_isready -h localhost -p "$PG_PORT" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
if ! pg_isready -h localhost -p "$PG_PORT" >/dev/null 2>&1; then
    echo "ERROR: postgres did not come up on port $PG_PORT"
    exit 1
fi

# brew creates a superuser named after the OS user. Connect as that user
# (no password on local socket) to bootstrap our dedicated role and DB.
SUPERUSER="$(whoami)"

echo "[2/4] ensuring role $DB_USER exists..."
psql -h localhost -p "$PG_PORT" -U "$SUPERUSER" -d postgres -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 \
    || psql -h localhost -p "$PG_PORT" -U "$SUPERUSER" -d postgres -c \
    "CREATE ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASS' CREATEDB"

echo "[3/4] ensuring database $DB_NAME exists..."
psql -h localhost -p "$PG_PORT" -U "$SUPERUSER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
    || psql -h localhost -p "$PG_PORT" -U "$SUPERUSER" -d postgres -c \
    "CREATE DATABASE $DB_NAME OWNER $DB_USER"

echo "[4/4] postgres ready."
echo ""
echo "DATABASE_URL=postgresql://$DB_USER:$DB_PASS@localhost:$PG_PORT/$DB_NAME"
