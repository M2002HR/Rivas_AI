#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${ADMIN_DB_URL:-}" && -n "${MYSQL_USER:-}" && -n "${MYSQL_PASSWORD:-}" && -n "${MYSQL_PORT:-}" && -n "${MYSQL_DATABASE:-}" ]]; then
  export ADMIN_DB_URL="mysql://${MYSQL_USER}:${MYSQL_PASSWORD}@127.0.0.1:${MYSQL_PORT}/${MYSQL_DATABASE}"
fi

CMD=(".venv/bin/python" "-m" "rivas.registration_admin")
if [[ "${1:-}" == "--once" ]]; then
  CMD+=("--once")
fi

PYTHONPATH="${PYTHONPATH:-src}" "${CMD[@]}"
