#!/usr/bin/env bash
# Project-local PostgreSQL 16 cluster on port 5433 (no sudo; leaves the system cluster on 5432 alone).
# Usage: scripts/dev-db.sh start|stop|status|reset
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PGDATA="$ROOT/.pgdata"
PORT="${PGPORT_DEV:-5433}"
BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
[ -x "$BIN/initdb" ] || { echo "PostgreSQL server binaries not found under /usr/lib/postgresql"; exit 1; }

init() {
  echo "[db] initialising cluster in $PGDATA"
  "$BIN/initdb" -D "$PGDATA" -U postgres --auth=trust -E UTF8 >/dev/null
}

start() {
  [ -d "$PGDATA" ] || init
  if "$BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then echo "[db] already running on $PORT"; else
    "$BIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/server.log" -o "-p $PORT -k /tmp -c listen_addresses=127.0.0.1" -w start >/dev/null
    echo "[db] started on 127.0.0.1:$PORT"
  fi
  for db in skincare skincare_test; do
    psql -h 127.0.0.1 -p "$PORT" -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='skincare'" | grep -q 1 \
      || psql -h 127.0.0.1 -p "$PORT" -U postgres -qc "CREATE ROLE skincare LOGIN PASSWORD 'skincare'"
    psql -h 127.0.0.1 -p "$PORT" -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
      || { psql -h 127.0.0.1 -p "$PORT" -U postgres -qc "CREATE DATABASE $db OWNER skincare"; echo "[db] created database $db"; }
  done
}

case "${1:-start}" in
  start) start ;;
  stop) "$BIN/pg_ctl" -D "$PGDATA" -m fast stop ;;
  status) "$BIN/pg_ctl" -D "$PGDATA" status ;;
  reset) "$BIN/pg_ctl" -D "$PGDATA" -m fast stop 2>/dev/null || true; rm -rf "$PGDATA"; start ;;
  *) echo "usage: $0 start|stop|status|reset"; exit 2 ;;
esac
