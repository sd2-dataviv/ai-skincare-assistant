#!/usr/bin/env bash
# Restart the API in demo mode (scripted agent, face check off) in the background. Logs: var/api.log
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID=$(ss -ltnp 2>/dev/null | awk '/127.0.0.1:8000 /{print $NF}' | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1 || true)
[ -n "${PID:-}" ] && kill "$PID" && sleep 1
mkdir -p "$ROOT/var"
cd "$ROOT/backend"
PHOTO_FACE_CHECK=false LLM_PROVIDER=scripted nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 > "$ROOT/var/api.log" 2>&1 &
for i in $(seq 1 20); do curl -sf http://127.0.0.1:8000/api/health >/dev/null && { echo "[api] up"; exit 0; }; sleep 0.5; done
echo "[api] failed to start; see var/api.log"; exit 1
