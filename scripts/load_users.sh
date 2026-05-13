#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${1:-users.json}"
WRITE_BACK="${WRITE_BACK:-1}"

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "users config not found: $CONFIG_PATH" >&2
  exit 1
fi

if [[ -z "${ADMIN_DB_URL:-}" && -n "${MYSQL_USER:-}" && -n "${MYSQL_PASSWORD:-}" && -n "${MYSQL_PORT:-}" && -n "${MYSQL_DATABASE:-}" ]]; then
  export ADMIN_DB_URL="mysql://${MYSQL_USER}:${MYSQL_PASSWORD}@127.0.0.1:${MYSQL_PORT}/${MYSQL_DATABASE}"
fi

CMD=(".venv/bin/python" "-m" "rivas.load_users" "--config" "$CONFIG_PATH")
if [[ "$WRITE_BACK" == "1" ]]; then
  CMD+=("--write-back")
fi

PYTHONPATH="${PYTHONPATH:-src}" "${CMD[@]}"
