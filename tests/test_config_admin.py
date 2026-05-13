from __future__ import annotations

from rivas.config import AdminConfig
from rivas.admin import _finalize_runtime_status, build_runtime_spec


def test_admin_config_uses_admin_db_url_override(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "hash_value")
    monkeypatch.setenv("MIRA_USERNAME", "@mira")
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "Z6N3m4wQ6vD7E0jS2b9P1rX8yK3oL5mN4tU7iA0cHfQ=")
    monkeypatch.setenv("TENANT_SERVICE_IMAGE", "mira_bot_api-mira-service:latest")
    monkeypatch.setenv("DOCKER_NETWORK_NAME", "mira_bot_api_default")
    monkeypatch.setenv("DB_URL", "mysql://app:app@mysql:3306/rivas")
    monkeypatch.setenv("ADMIN_DB_URL", "mysql://admin:admin@127.0.0.1:3307/rivas")

    cfg = AdminConfig.from_env()
    assert cfg.db.mysql_host == "127.0.0.1"
    assert cfg.db.mysql_port == 3307
    assert cfg.db.mysql_user == "admin"
    assert cfg.notify_user_activation is True
    assert cfg.bale_bot_token is None


def test_admin_config_proxy_fields(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "hash_value")
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "Z6N3m4wQ6vD7E0jS2b9P1rX8yK3oL5mN4tU7iA0cHfQ=")
    monkeypatch.setenv("DB_URL", "mysql://app:app@mysql:3306/rivas")
    monkeypatch.setenv("TG_PROXY_ENABLED", "true")
    monkeypatch.setenv("TG_PROXY_TYPE", "socks5")
    monkeypatch.setenv("TG_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("TG_PROXY_PORT", "1080")
    monkeypatch.setenv("TG_PROXY_USERNAME", "u")
    monkeypatch.setenv("TG_PROXY_PASSWORD", "p")
    monkeypatch.setenv("TG_PROXY_RDNS", "false")

    cfg = AdminConfig.from_env()
    assert cfg.tg_proxy_enabled is True
    assert cfg.tg_proxy_type == "socks5"
    assert cfg.tg_proxy_host == "127.0.0.1"
    assert cfg.tg_proxy_host_runtime == "127.0.0.1"
    assert cfg.tg_proxy_host_admin == "127.0.0.1"
    assert cfg.tg_proxy_port == 1080
    assert cfg.tg_proxy_username == "u"
    assert cfg.tg_proxy_password == "p"
    assert cfg.tg_proxy_rdns is False


def test_runtime_spec_contains_proxy_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "hash_value")
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "Z6N3m4wQ6vD7E0jS2b9P1rX8yK3oL5mN4tU7iA0cHfQ=")
    monkeypatch.setenv("DB_URL", "mysql://app:app@mysql:3306/rivas")
    monkeypatch.setenv("TG_PROXY_ENABLED", "true")
    monkeypatch.setenv("TG_PROXY_TYPE", "http")
    monkeypatch.setenv("TG_PROXY_HOST", "10.0.0.1")
    monkeypatch.setenv("TG_PROXY_PORT", "9000")
    monkeypatch.setenv("TG_PROXY_RDNS", "true")

    cfg = AdminConfig.from_env()
    spec = build_runtime_spec(
        config=cfg,
        container_name="c1",
        network_alias="a1",
        session="sess",
        tg_api_id=12345,
        tg_api_hash="hash_value",
    )
    assert spec.envs["TG_PROXY_ENABLED"] == "true"
    assert spec.envs["TG_PROXY_TYPE"] == "http"
    assert spec.envs["TG_PROXY_HOST"] == "10.0.0.1"
    assert spec.envs["TG_PROXY_PORT"] == "9000"
    assert spec.envs["TG_PROXY_RDNS"] == "true"


def test_admin_proxy_host_defaults_for_host_docker_internal(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "hash_value")
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "Z6N3m4wQ6vD7E0jS2b9P1rX8yK3oL5mN4tU7iA0cHfQ=")
    monkeypatch.setenv("DB_URL", "mysql://app:app@mysql:3306/rivas")
    monkeypatch.setenv("TG_PROXY_ENABLED", "true")
    monkeypatch.setenv("TG_PROXY_TYPE", "http")
    monkeypatch.setenv("TG_PROXY_HOST", "host.docker.internal")
    monkeypatch.setenv("TG_PROXY_PORT", "9000")

    cfg = AdminConfig.from_env()
    assert cfg.tg_proxy_host_runtime == "host.docker.internal"
    assert cfg.tg_proxy_host_admin == "127.0.0.1"


def test_finalize_runtime_status():
    assert _finalize_runtime_status("created", True) == "running"
    assert _finalize_runtime_status("recreated", True) == "running"
    assert _finalize_runtime_status("running", True) == "running"
    assert _finalize_runtime_status("created", False) == "starting"
    assert _finalize_runtime_status("running", False) == "starting"
    assert _finalize_runtime_status("stopped", False) == "stopped"
