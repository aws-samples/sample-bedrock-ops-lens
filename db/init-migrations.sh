#!/usr/bin/env bash
# Runs after schema.sql when the local PostgreSQL container is first initialized.
set -euo pipefail
for migration in /docker-entrypoint-initdb.d/migrations/*.sql; do
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
        --file "$migration"
done
