from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from .config import AdminConfig, ConfigError
from .crypto_utils import SecretBox
from .logging_utils import get_logger, log_event, setup_logging
from .storage import Storage
from .tenant_runtime import TenantRuntimeSpec, build_runtime_names, check_container_ready, ensure_tenant_container


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        config = AdminConfig.from_env()
    except ConfigError as exc:
        print(f"[config-error] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    setup_logging(config.log_level)
    log = get_logger("rivas.admin")

    if args.command == "tenant-add":
        asyncio.run(_tenant_add(args, config, log))
        return

    if args.command == "tenant-list":
        asyncio.run(_tenant_list(config))
        return

    if args.command == "tenant-disable":
        asyncio.run(_tenant_set_status(config, args.tenant_id, "disabled"))
        return

    if args.command == "tenant-enable":
        asyncio.run(_tenant_set_status(config, args.tenant_id, "active"))
        return

    raise SystemExit(1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rivas admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("tenant-add", help="Create tenant and provision dedicated Mira service")
    p_add.add_argument("--tenant-slug", required=True)
    p_add.add_argument("--owner-name", required=True)
    p_add.add_argument("--phone", required=True, help="Telegram phone with country code, e.g. +989...")
    p_add.add_argument("--bale-user-id", required=True)
    p_add.add_argument("--bale-chat-id", required=True)
    p_add.add_argument("--tenant-id", required=False)
    p_add.add_argument("--string-session", required=False, help="Use existing Telethon StringSession and skip login code flow")
    p_add.add_argument("--tg-api-id", required=False, help="Optional override for tenant Telegram API ID")
    p_add.add_argument("--tg-api-hash", required=False, help="Optional override for tenant Telegram API hash")

    sub.add_parser("tenant-list", help="List all tenants")

    p_dis = sub.add_parser("tenant-disable", help="Disable tenant")
    p_dis.add_argument("--tenant-id", required=True)

    p_en = sub.add_parser("tenant-enable", help="Enable tenant")
    p_en.add_argument("--tenant-id", required=True)

    return parser


async def _tenant_add(args: argparse.Namespace, config: AdminConfig, log) -> None:
    tenant_id = args.tenant_id or f"tenant_{uuid.uuid4().hex[:14]}"
    tenant_slug = args.tenant_slug.strip().lower()
    container_name, network_alias, endpoint_base_url = build_runtime_names(tenant_slug)

    tg_api_id = int(args.tg_api_id) if args.tg_api_id else config.tg_api_id
    tg_api_hash = args.tg_api_hash.strip() if args.tg_api_hash else config.tg_api_hash

    storage = Storage(config.db, retention_days=30)
    await storage.init()

    try:
        existing = await storage.find_active_tenant_by_phone(args.phone, tg_api_id)
        if existing and str(existing.get("tenant_id") or "") != tenant_id:
            existing_status = str(existing.get("status") or "unknown")
            existing_runtime = str(existing.get("runtime_status") or "unknown")
            raise RuntimeError(
                "This Telegram account is already linked to another tenant. "
                f"tenant_id={existing.get('tenant_id')} status={existing_status} runtime={existing_runtime}. "
                "Using one session in multiple running containers causes Telegram AuthKey duplication."
            )

        if args.string_session:
            session = await _validate_existing_session(
                tg_api_id=tg_api_id,
                tg_api_hash=tg_api_hash,
                session=args.string_session.strip(),
                config=config,
            )
        else:
            session = await _collect_telethon_session(
                phone=args.phone,
                tg_api_id=tg_api_id,
                tg_api_hash=tg_api_hash,
                config=config,
            )

        box = SecretBox(config.master_encryption_key)
        tg_api_hash_encrypted = box.encrypt(tg_api_hash)
        tg_session_encrypted = box.encrypt(session)

        await storage.upsert_tenant(tenant_id=tenant_id, tenant_slug=tenant_slug, owner_name=args.owner_name, status="active")
        await storage.upsert_tenant_credentials(
            tenant_id=tenant_id,
            phone_e164=args.phone,
            tg_api_id=tg_api_id,
            tg_api_hash_encrypted=tg_api_hash_encrypted,
            tg_session_encrypted=tg_session_encrypted,
            encryption_version="fernet-v1",
        )

        runtime_spec = build_runtime_spec(
            config=config,
            container_name=container_name,
            network_alias=network_alias,
            session=session,
            tg_api_id=tg_api_id,
            tg_api_hash=tg_api_hash,
        )
        _, runtime_status = ensure_tenant_container(runtime_spec)
        ready = check_container_ready(container_name, timeout_seconds=35)
        runtime_status = _finalize_runtime_status(runtime_status, ready)

        await storage.upsert_tenant_runtime(
            tenant_id=tenant_id,
            container_name=container_name,
            endpoint_base_url=endpoint_base_url,
            runtime_status=runtime_status,
            service_port=8090,
        )
        await storage.bind_bale_user(tenant_id=tenant_id, bale_user_id=args.bale_user_id, bale_chat_id=args.bale_chat_id)
        await storage.insert_audit_event(
            tenant_id=tenant_id,
            actor="admin-cli",
            event_type="tenant_add",
            payload={
                "tenant_id": tenant_id,
                "tenant_slug": tenant_slug,
                "container_name": container_name,
                "network_alias": network_alias,
                "endpoint_base_url": endpoint_base_url,
                "bale_user_id": args.bale_user_id,
                "bale_chat_id": args.bale_chat_id,
            },
        )
        log_event(log, "tenant_add_success", tenant_id=tenant_id, tenant_slug=tenant_slug, container_name=container_name)
    finally:
        await storage.close()

    print("Tenant created successfully")
    print(f"tenant_id={tenant_id}")
    print(f"endpoint_base_url={endpoint_base_url}")
    print(f"container_name={container_name}")


async def _collect_telethon_session(
    *,
    phone: str,
    tg_api_id: int,
    tg_api_hash: str,
    config: AdminConfig,
) -> str:
    client = TelegramClient(StringSession(), tg_api_id, tg_api_hash, proxy=_build_proxy_tuple(config))
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        print(f"Code sent to {phone}")
        code = input("Enter Telegram login code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = input("Enter Telegram 2FA password: ").strip()
            await client.sign_in(password=password)

        if not await client.is_user_authorized():
            raise RuntimeError("Telethon login failed: user is not authorized")

        session = client.session.save()
        if not session:
            raise RuntimeError("Failed to save Telegram session")
        return str(session)
    finally:
        await client.disconnect()


async def _validate_existing_session(*, tg_api_id: int, tg_api_hash: str, session: str, config: AdminConfig) -> str:
    if not session:
        raise RuntimeError("Provided string session is empty")
    client = TelegramClient(StringSession(session), tg_api_id, tg_api_hash, proxy=_build_proxy_tuple(config))
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Provided string session is not authorized")
        return session
    finally:
        await client.disconnect()


def build_runtime_spec(
    *,
    config: AdminConfig,
    container_name: str,
    network_alias: str,
    session: str,
    tg_api_id: int,
    tg_api_hash: str,
) -> TenantRuntimeSpec:
    envs: dict[str, str] = {
        "TG_API_ID": str(tg_api_id),
        "TG_API_HASH": tg_api_hash,
        "TG_STRING_SESSION": session,
        "MIRA_USERNAME": config.mira_username,
        "MIRA_SERVICE_API_KEY": config.shared_mira_service_api_key or "",
        "APP_ENV": config.app_env,
        "LOG_LEVEL": config.log_level,
        "QUIET_WINDOW_SECONDS": str(config.default_quiet_window_seconds),
        "FIRST_RESPONSE_TIMEOUT_SECONDS": str(config.default_first_response_timeout_seconds),
        "OVERALL_TIMEOUT_SECONDS": str(config.default_overall_timeout_seconds),
        "FLOODWAIT_HARD_LIMIT_SECONDS": str(config.default_floodwait_hard_limit_seconds),
        "MIRA_MAX_PAYLOAD_MB": str(config.default_max_payload_mb),
        "MIRA_API_HOST": "0.0.0.0",
        "MIRA_API_PORT": "8090",
        "TG_PROXY_ENABLED": "true" if config.tg_proxy_enabled else "false",
        "TG_PROXY_TYPE": config.tg_proxy_type,
        "TG_PROXY_HOST": config.tg_proxy_host_runtime or "",
        "TG_PROXY_PORT": str(config.tg_proxy_port or ""),
        "TG_PROXY_USERNAME": config.tg_proxy_username or "",
        "TG_PROXY_PASSWORD": config.tg_proxy_password or "",
        "TG_PROXY_RDNS": "true" if config.tg_proxy_rdns else "false",
    }
    return TenantRuntimeSpec(
        container_name=container_name,
        network_name=config.docker_network_name,
        network_alias=network_alias,
        image=config.tenant_container_image,
        envs=envs,
        command=["mira-telegram-service-api"],
    )


def _safe_row(row: dict[str, object]) -> dict[str, object]:
    safe = dict(row)
    for key in list(safe.keys()):
        lowered = key.lower()
        if "session" in lowered or "token" in lowered or "hash" in lowered or "password" in lowered:
            safe[key] = "***"
    return safe


async def _tenant_list(config: AdminConfig) -> None:
    storage = Storage(config.db, retention_days=30)
    await storage.init()
    try:
        rows = await storage.list_tenants()
    finally:
        await storage.close()

    if not rows:
        print("No tenants found")
        return

    for row in rows:
        print(_safe_row(row))


async def _tenant_set_status(config: AdminConfig, tenant_id: str, status: str) -> None:
    storage = Storage(config.db, retention_days=30)
    await storage.init()
    try:
        await storage.set_tenant_status(tenant_id, status)
        await storage.insert_audit_event(tenant_id=tenant_id, actor="admin-cli", event_type="tenant_status", payload={"status": status})
    finally:
        await storage.close()

    print(f"tenant {tenant_id} status set to {status}")


def _build_proxy_tuple(config: AdminConfig):
    if not config.tg_proxy_enabled:
        return None
    if not config.tg_proxy_host_admin:
        raise RuntimeError("TG_PROXY_ENABLED=true but TG_PROXY_HOST_ADMIN/TG_PROXY_HOST is missing")
    if not config.tg_proxy_port:
        raise RuntimeError("TG_PROXY_ENABLED=true but TG_PROXY_PORT is missing")
    proxy_type = (config.tg_proxy_type or "").lower().strip()
    if proxy_type not in {"socks5", "socks4", "http"}:
        raise RuntimeError("TG_PROXY_TYPE must be one of: socks5, socks4, http")
    return (
        proxy_type,
        config.tg_proxy_host_admin,
        int(config.tg_proxy_port),
        bool(config.tg_proxy_rdns),
        config.tg_proxy_username,
        config.tg_proxy_password,
    )


def _finalize_runtime_status(initial_status: str, ready: bool) -> str:
    status = (initial_status or "").strip().lower()
    if ready:
        return "running"
    if status in {"created", "recreated", "running"}:
        return "starting"
    return status or "starting"


if __name__ == "__main__":
    main()
