from __future__ import annotations

import json
import logging
from collections.abc import Mapping

_SENSITIVE_HINTS = (
    "token",
    "secret",
    "session",
    "password",
    "api_hash",
    "authorization",
)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    safe = _sanitize(fields)
    payload = json.dumps(safe, ensure_ascii=False, sort_keys=True)
    logger.info("%s | %s", event, payload)


def _sanitize(value: object, key_name: str | None = None) -> object:
    if key_name and any(hint in key_name.lower() for hint in _SENSITIVE_HINTS):
        return "***"

    if isinstance(value, Mapping):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_sanitize(v) for v in value]

    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"

    return value
