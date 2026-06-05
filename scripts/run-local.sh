#!/usr/bin/env bash
# ============================================================================
# Bedrock Ops Lens — local dev launcher.
#
# Brings up Postgres, backend, and frontend in the right order. Survives
# rebuilds: kills any existing services on our ports first.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== Bedrock Ops Lens local =="
echo "    project root: $ROOT"

# 1. Postgres
"$ROOT/db/local-setup.sh"

# 2. Backend on 8001 (8000 is often taken)
echo "[backend] killing any prior instance on 8001..."
PIDS=$(lsof -nP -iTCP:8001 -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "${PIDS}" ]]; then kill ${PIDS} || true; sleep 1; fi

echo "[backend] starting uvicorn on 8001..."
( cd "$ROOT/backend" && PYTHONPATH=. "$ROOT/.venv/bin/uvicorn" app.main:app \
    --host 127.0.0.1 --port 8001 --log-level warning >/tmp/bedrock-lens-backend.log 2>&1 & )
for i in {1..20}; do
    if "$ROOT/.venv/bin/python" -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8001/api/health',timeout=1)" 2>/dev/null; then
        echo "[backend] ready"; break
    fi; sleep 1
done

# 3. Frontend on 5173
echo "[frontend] killing any prior instance on 5173..."
PIDS=$(lsof -nP -iTCP:5173 -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "${PIDS}" ]]; then kill ${PIDS} || true; sleep 1; fi

echo "[frontend] starting vite on 5173..."
( cd "$ROOT/frontend" && nohup npm run dev >/tmp/bedrock-lens-frontend.log 2>&1 & )

echo ""
echo "Open http://localhost:5173/ — backend logs at /tmp/bedrock-lens-backend.log"
