from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ._submodule_loader import ensure_mira_submodule_importable


class ConfigError(RuntimeError):
    """Raised when required environment variables are missing or invalid."""


@dataclass(slots=True)
class DatabaseConfig:
    backend: str
    db_url: str
    sqlite_path: Path | None
    mysql_host: str | None
    mysql_port: int | None
    mysql_user: str | None
    mysql_password: str | None
    mysql_database: str | None


@dataclass(slots=True)
class BotConfig:
    bale_bot_token: str
    app_env: str
    log_level: str
    db: DatabaseConfig
    retention_days: int

    max_photo_mb: int
    max_audio_mb: int
    max_text_chunk_chars: int
    status_ping_seconds: int
    request_timeout_seconds: int
    tenant_health_interval_seconds: int
    tenant_health_timeout_seconds: int

    health_host: str
    health_port: int

    default_mira_service_url: str
    mira_service_api_key: str | None

    log_tg_enabled: bool
    log_tg_bot_token: str | None
    log_tg_chat_username: str | None
    log_tg_chat_id: str | None
    log_tg_chat_target: str | None
    log_tg_min_level: str

    @property
    def max_photo_bytes(self) -> int:
        return self.max_photo_mb * 1024 * 1024

    @property
    def max_audio_bytes(self) -> int:
        return self.max_audio_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> "BotConfig":
        db_url = os.getenv("DB_URL", "sqlite:///data/rivas.db").strip() or "sqlite:///data/rivas.db"
        db_cfg = _parse_db_config(db_url)

        return cls(
            bale_bot_token=_required("BALE_BOT_TOKEN"),
            app_env=os.getenv("APP_ENV", "production").strip() or "production",
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            db=db_cfg,
            retention_days=_positive_int("RETENTION_DAYS", 30),
            max_photo_mb=_positive_int("MAX_PHOTO_MB", 10),
            max_audio_mb=_positive_int("MAX_AUDIO_MB", 20),
            max_text_chunk_chars=_positive_int("MAX_TEXT_CHUNK_CHARS", 3900),
            status_ping_seconds=_positive_int("STATUS_PING_SECONDS", 12),
            request_timeout_seconds=_positive_int("REQUEST_TIMEOUT_SECONDS", 320),
            tenant_health_interval_seconds=_positive_int("TENANT_HEALTH_INTERVAL_SECONDS", 15),
            tenant_health_timeout_seconds=_positive_int("TENANT_HEALTH_TIMEOUT_SECONDS", 6),
            health_host=os.getenv("BOT_HEALTH_HOST", "0.0.0.0").strip() or "0.0.0.0",
            health_port=_positive_int("BOT_HEALTH_PORT", 8080),
            default_mira_service_url=(os.getenv("MIRA_SERVICE_URL", "http://mira-service:8090").strip() or "http://mira-service:8090").rstrip("/"),
            mira_service_api_key=_optional("MIRA_SERVICE_API_KEY"),
            log_tg_enabled=_bool_env("LOG_TG_ENABLED", False),
            log_tg_bot_token=_optional("LOG_TG_BOT_TOKEN"),
            log_tg_chat_username=_normalize_chat_username(_optional("LOG_TG_CHAT_USERNAME")),
            log_tg_chat_id=_optional("LOG_TG_CHAT_ID"),
            log_tg_chat_target=_select_chat_target(
                username=_normalize_chat_username(_optional("LOG_TG_CHAT_USERNAME")),
                chat_id=_optional("LOG_TG_CHAT_ID"),
            ),
            log_tg_min_level=os.getenv("LOG_TG_MIN_LEVEL", "WARNING").strip().upper() or "WARNING",
        )


@dataclass(slots=True)
class AdminConfig:
    app_env: str
    log_level: str
    db: DatabaseConfig

    tg_api_id: int
    tg_api_hash: str
    mira_username: str

    default_quiet_window_seconds: int
    default_first_response_timeout_seconds: int
    default_overall_timeout_seconds: int
    default_floodwait_hard_limit_seconds: int
    default_max_payload_mb: int

    tenant_container_image: str
    docker_network_name: str

    shared_mira_service_api_key: str | None
    master_encryption_key: str
    bale_bot_token: str | None
    notify_user_activation: bool
    tg_proxy_enabled: bool
    tg_proxy_type: str
    tg_proxy_host: str | None
    tg_proxy_host_runtime: str | None
    tg_proxy_host_admin: str | None
    tg_proxy_port: int | None
    tg_proxy_username: str | None
    tg_proxy_password: str | None
    tg_proxy_rdns: bool

    @classmethod
    def from_env(cls) -> "AdminConfig":
        db_url = (
            os.getenv("ADMIN_DB_URL")
            or os.getenv("DB_URL")
            or "mysql://rivas:rivas@mysql:3306/rivas"
        ).strip()
        db_cfg = _parse_db_config(db_url)
        if db_cfg.backend != "mysql":
            raise ConfigError("Admin provisioning requires MySQL DB_URL")
        proxy_host = _optional("TG_PROXY_HOST")
        proxy_host_runtime = _optional("TG_PROXY_HOST_RUNTIME") or proxy_host
        proxy_host_admin = _optional("TG_PROXY_HOST_ADMIN") or _default_admin_proxy_host(proxy_host)

        return cls(
            app_env=os.getenv("APP_ENV", "production").strip() or "production",
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            db=db_cfg,
            tg_api_id=_required_int("TG_API_ID"),
            tg_api_hash=_required("TG_API_HASH"),
            mira_username=os.getenv("MIRA_USERNAME", "@mira").strip() or "@mira",
            default_quiet_window_seconds=_positive_int("QUIET_WINDOW_SECONDS", 4),
            default_first_response_timeout_seconds=_positive_int("FIRST_RESPONSE_TIMEOUT_SECONDS", 60),
            default_overall_timeout_seconds=_positive_int("OVERALL_TIMEOUT_SECONDS", 240),
            default_floodwait_hard_limit_seconds=_positive_int("FLOODWAIT_HARD_LIMIT_SECONDS", 120),
            default_max_payload_mb=_positive_int("MIRA_MAX_PAYLOAD_MB", 20),
            tenant_container_image=os.getenv("TENANT_SERVICE_IMAGE", "mira_bot_api-mira-service:latest").strip() or "mira_bot_api-mira-service:latest",
            docker_network_name=os.getenv("DOCKER_NETWORK_NAME", "mira_bot_api_default").strip() or "mira_bot_api_default",
            shared_mira_service_api_key=_optional("MIRA_SERVICE_API_KEY"),
            master_encryption_key=_required("MASTER_ENCRYPTION_KEY"),
            bale_bot_token=_optional("BALE_BOT_TOKEN"),
            notify_user_activation=_bool_env("NOTIFY_USER_ACTIVATION", True),
            tg_proxy_enabled=_bool_env("TG_PROXY_ENABLED", False),
            tg_proxy_type=(os.getenv("TG_PROXY_TYPE", "socks5").strip().lower() or "socks5"),
            tg_proxy_host=proxy_host,
            tg_proxy_host_runtime=proxy_host_runtime,
            tg_proxy_host_admin=proxy_host_admin,
            tg_proxy_port=_optional_int("TG_PROXY_PORT"),
            tg_proxy_username=_optional("TG_PROXY_USERNAME"),
            tg_proxy_password=_optional("TG_PROXY_PASSWORD"),
            tg_proxy_rdns=_bool_env("TG_PROXY_RDNS", True),
        )


def _parse_db_config(db_url: str) -> DatabaseConfig:
    raw = db_url.strip()
    if raw.startswith("sqlite:///"):
        path = raw.removeprefix("sqlite:///")
        return DatabaseConfig(
            backend="sqlite",
            db_url=raw,
            sqlite_path=Path(path),
            mysql_host=None,
            mysql_port=None,
            mysql_user=None,
            mysql_password=None,
            mysql_database=None,
        )

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"mysql", "mysql+pymysql"}:
        raise ConfigError("DB_URL must be sqlite:///... or mysql://user:pass@host:port/db")

    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 3306)
    user = parsed.username or _required("MYSQL_USER")
    password = parsed.password or _required("MYSQL_PASSWORD")
    database = (parsed.path or "").lstrip("/") or _required("MYSQL_DATABASE")

    return DatabaseConfig(
        backend="mysql",
        db_url=raw,
        sqlite_path=None,
        mysql_host=host,
        mysql_port=port,
        mysql_user=user,
        mysql_password=password,
        mysql_database=database,
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


def _required_int(key: str) -> int:
    value = _required(key)
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"Env var {key} must be int") from exc


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


def _bool_env(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Env var {key} must be boolean")


def _optional_int(key: str) -> int | None:
    raw = os.getenv(key)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"Env var {key} must be int") from exc
    if value <= 0:
        raise ConfigError(f"Env var {key} must be > 0")
    return value


def _default_admin_proxy_host(host: str | None) -> str | None:
    if host is None:
        return None
    if host.strip().lower() == "host.docker.internal":
        # host-side scripts cannot reliably resolve this alias on Linux hosts.
        return "127.0.0.1"
    return host


def _normalize_chat_username(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.startswith("@"):
        return raw
    return f"@{raw}"


def _select_chat_target(*, username: str | None, chat_id: str | None) -> str | None:
    if username:
        return username
    if chat_id:
        return chat_id.strip() or None
    return None


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
