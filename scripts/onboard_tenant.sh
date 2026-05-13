#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 5 ]]; then
  echo "Usage: $0 <tenant-slug> <owner-name> <phone> <bale-user-id> <bale-chat-id> [tenant-id]" >&2
  echo "Optional envs: TG_STRING_SESSION, TENANT_TG_API_ID, TENANT_TG_API_HASH" >&2
  exit 1
fi

TENANT_SLUG="$1"
OWNER_NAME="$2"
PHONE="$3"
BALE_USER_ID="$4"
BALE_CHAT_ID="$5"
TENANT_ID="${6:-}"

CMD=(rivas-admin tenant-add \
  --tenant-slug "$TENANT_SLUG" \
  --owner-name "$OWNER_NAME" \
  --phone "$PHONE" \
  --bale-user-id "$BALE_USER_ID" \
  --bale-chat-id "$BALE_CHAT_ID")

if [[ -n "$TENANT_ID" ]]; then
  CMD+=(--tenant-id "$TENANT_ID")
fi

if [[ -n "${TG_STRING_SESSION:-}" ]]; then
  CMD+=(--string-session "$TG_STRING_SESSION")
fi

if [[ -n "${TENANT_TG_API_ID:-}" ]]; then
  CMD+=(--tg-api-id "$TENANT_TG_API_ID")
fi

if [[ -n "${TENANT_TG_API_HASH:-}" ]]; then
  CMD+=(--tg-api-hash "$TENANT_TG_API_HASH")
fi

if ! command -v rivas-admin >/dev/null 2>&1; then
  if [[ -x ".venv/bin/python" ]]; then
    CMD=(".venv/bin/python" "-m" "rivas.admin" "${CMD[@]:1}")
    export PYTHONPATH="${PYTHONPATH:-src}"
  else
    echo "rivas-admin is not installed and .venv/bin/python was not found." >&2
    exit 1
  fi
fi

"${CMD[@]}"
