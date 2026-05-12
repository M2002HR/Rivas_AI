from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ._submodule_loader import ensure_mira_submodule_importable


class ConfigError(RuntimeError):
    """Raised when required environment variables are missing or invalid."""


@dataclass(slots=True)
class BotConfig:
    bale_bot_token: str
    mira_service_url: str
    mira_service_api_key: str | None
    app_env: str
    log_level: str
    db_url: str
    retention_days: int
    max_photo_mb: int
    max_audio_mb: int
    max_text_chunk_chars: int
    status_ping_seconds: int
    request_timeout_seconds: int
    health_host: str
    health_port: int

    @property
    def max_photo_bytes(self) -> int:
        return self.max_photo_mb * 1024 * 1024

    @property
    def max_audio_bytes(self) -> int:
        return self.max_audio_mb * 1024 * 1024

    @property
    def sqlite_path(self) -> Path:
        raw = self.db_url.strip()
        if raw.startswith("sqlite:///"):
            value = raw.removeprefix("sqlite:///")
            return Path(value)
        if raw.startswith("sqlite://"):
            value = raw.removeprefix("sqlite://")
            return Path(value)
        return Path(raw)

    @classmethod
    def from_env(cls) -> "BotConfig":
        return cls(
            bale_bot_token=_required("BALE_BOT_TOKEN"),
            mira_service_url=(os.getenv("MIRA_SERVICE_URL", "http://127.0.0.1:8090").strip() or "http://127.0.0.1:8090").rstrip("/"),
            mira_service_api_key=_optional("MIRA_SERVICE_API_KEY"),
            app_env=os.getenv("APP_ENV", "production").strip() or "production",
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            db_url=os.getenv("DB_URL", "sqlite:///data/rivas.db").strip() or "sqlite:///data/rivas.db",
            retention_days=_positive_int("RETENTION_DAYS", 7),
            max_photo_mb=_positive_int("MAX_PHOTO_MB", 10),
            max_audio_mb=_positive_int("MAX_AUDIO_MB", 20),
            max_text_chunk_chars=_positive_int("MAX_TEXT_CHUNK_CHARS", 3900),
            status_ping_seconds=_positive_int("STATUS_PING_SECONDS", 12),
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT_SECONDS", 320),
            health_host=os.getenv("BOT_HEALTH_HOST", "0.0.0.0").strip() or "0.0.0.0",
            health_port=_positive_int("BOT_HEALTH_PORT", 8080),
        )


def _required(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ConfigError(f"Missing required env var: {key}")
    return value


def _optional(key: str) -> str | None:
    value = os.getenv(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _positive_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"Env var {key} must be int") from exc
    if value <= 0:
        raise ConfigError(f"Env var {key} must be > 0")
    return value


ensure_mira_submodule_importable()

try:
    from mira_telegram_service.config import MiraServiceConfig as MiraServiceConfig
except Exception:  # pragma: no cover
    class MiraServiceConfig:  # type: ignore[no-redef]
        @classmethod
        def from_env(cls):
            raise ConfigError(
                "mira-telegram-service package is not installed or importable. "
                "Install submodule package before starting Mira API service."
            )
