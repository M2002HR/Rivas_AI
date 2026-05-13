from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .admin import _collect_telethon_session, _finalize_runtime_status, build_runtime_spec
from .bale_notifier import BaleNotifyError, send_text_message
from .config import AdminConfig, ConfigError
from .crypto_utils import SecretBox
from .logging_utils import get_logger, log_event, setup_logging
from .models import RegistrationStatus
from .storage import Storage
from .tenant_runtime import build_runtime_names, check_container_ready, ensure_tenant_container, stop_and_remove_tenant_container

PHONE_RE = re.compile(r"^\+[1-9]\d{9,14}$")
ACTIVATION_TEXT = (
    "ثبت‌نامت با موفقیت فعال شد.\n"
    "از الان می‌تونی با ریواس کار کنی.\n"
    "برای شروع، /start رو بزن."
)


@dataclass(slots=True)
class DesiredUser:
    tenant_id: str
    tenant_slug: str
    owner_name: str
    status: str
    bale_user_id: str
    bale_chat_id: str
    phone_e164: str
    tg_api_id: int
    tg_api_hash: str
    tg_string_session: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync users.json to DB + tenant containers")
    parser.add_argument("--config", default="users.json", help="Path to users.json")
    parser.add_argument("--write-back", action="store_true", help="Write generated string_session back to users.json")
    args = parser.parse_args()

    try:
        cfg = AdminConfig.from_env()
    except ConfigError as exc:
        print(f"[config-error] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    setup_logging(cfg.log_level)
    log = get_logger("rivas.load_users")
    asyncio.run(_run(args.config, args.write_back, cfg, log))


async def _run(config_path: str, write_back: bool, cfg: AdminConfig, log) -> None:
    path = Path(config_path)
    raw = _read_users_file(path)
    desired_users = await _normalize_users(raw, cfg, log)

    storage = Storage(cfg.db, retention_days=30)
    await storage.init()

    try:
        existing = {str(row.get("id")): row for row in await storage.list_tenants()}
        seen_tenants: set[str] = set()

        box = SecretBox(cfg.master_encryption_key)
        for user in desired_users:
            seen_tenants.add(user.tenant_id)
            await _sync_single_user(storage, cfg, user, box, log)

        for tenant_id, row in existing.items():
            if tenant_id in seen_tenants:
                continue
            await _disable_removed_tenant(storage, tenant_id, row, log)
    finally:
        await storage.close()

    if write_back:
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log_event(log, "users_config_written", path=str(path))

    print("users sync completed")


def _read_users_file(path: Path) -> dict[str, object]:
    if not path.exists():
        raise RuntimeError(f"users config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("users.json must be a JSON object")
    if "users" not in data or not isinstance(data["users"], list):
        raise RuntimeError("users.json must include users[]")
    return data


async def _normalize_users(raw: dict[str, object], cfg: AdminConfig, log) -> list[DesiredUser]:
    normalized: list[DesiredUser] = []
    users = raw.get("users") or []
    seen_ids: set[str] = set()
    seen_phone_api: set[tuple[str, int]] = set()
    for i, item in enumerate(users):
        if not isinstance(item, dict):
            raise RuntimeError(f"users[{i}] must be an object")

        tenant_slug = str(item.get("tenant_slug") or "").strip().lower()
        if not tenant_slug:
            raise RuntimeError(f"users[{i}].tenant_slug is required")
        tenant_id = str(item.get("tenant_id") or f"tenant_{_slug_to_id(tenant_slug)}").strip()
        if tenant_id in seen_ids:
            raise RuntimeError(f"Duplicate tenant_id in users.json: {tenant_id}")
        seen_ids.add(tenant_id)

        owner_name = str(item.get("owner_name") or tenant_slug).strip()
        status = str(item.get("status") or "active").strip().lower()
        if status not in {"active", "disabled"}:
            raise RuntimeError(f"users[{i}].status must be active|disabled")

        bale_user_id = str(item.get("bale_user_id") or "").strip()
        bale_chat_id = str(item.get("bale_chat_id") or "").strip()
        phone = str(item.get("phone_e164") or "").strip()
        if not bale_user_id or not bale_chat_id:
            raise RuntimeError(f"users[{i}] bale_user_id/bale_chat_id are required")
        if not PHONE_RE.match(phone):
            raise RuntimeError(f"users[{i}].phone_e164 must be E.164 like +989...")

        tg = item.get("telegram") or {}
        if not isinstance(tg, dict):
            raise RuntimeError(f"users[{i}].telegram must be object")

        tg_api_id = int(tg.get("api_id") or cfg.tg_api_id)
        tg_api_hash = str(tg.get("api_hash") or cfg.tg_api_hash).strip()
        if not tg_api_hash:
            raise RuntimeError(f"users[{i}].telegram.api_hash is required")
        tg_string_session = str(tg.get("string_session") or "").strip()

        if status == "active" and not tg_string_session:
            log_event(log, "collecting_session", tenant_id=tenant_id, phone=phone)
            tg_string_session = await _collect_telethon_session(
                phone=phone,
                tg_api_id=tg_api_id,
                tg_api_hash=tg_api_hash,
                config=cfg,
            )
            tg["string_session"] = tg_string_session
            item["telegram"] = tg

        if status == "active":
            key = (phone, tg_api_id)
            if key in seen_phone_api:
                raise RuntimeError(f"Duplicate active phone/api_id in users.json: phone={phone}, api_id={tg_api_id}")
            seen_phone_api.add(key)

        normalized.append(
            DesiredUser(
                tenant_id=tenant_id,
                tenant_slug=tenant_slug,
                owner_name=owner_name,
                status=status,
                bale_user_id=bale_user_id,
                bale_chat_id=bale_chat_id,
                phone_e164=phone,
                tg_api_id=tg_api_id,
                tg_api_hash=tg_api_hash,
                tg_string_session=tg_string_session,
            )
        )
    return normalized


async def _sync_single_user(storage: Storage, cfg: AdminConfig, user: DesiredUser, box: SecretBox, log) -> None:
    container_name, network_alias, endpoint_base_url = build_runtime_names(user.tenant_slug)
    digest = _config_digest(cfg, user, container_name, network_alias)
    previous = await storage.get_tenant_sync_state(user.tenant_id)
    changed = previous != digest

    await storage.upsert_tenant(
        tenant_id=user.tenant_id,
        tenant_slug=user.tenant_slug,
        owner_name=user.owner_name,
        status=user.status,
    )

    if user.status != "active":
        await storage.deactivate_bindings_for_tenant(user.tenant_id)
        stop_and_remove_tenant_container(container_name)
        await storage.set_tenant_status(user.tenant_id, "disabled")
        await storage.upsert_tenant_runtime(
            tenant_id=user.tenant_id,
            container_name=container_name,
            endpoint_base_url=endpoint_base_url,
            runtime_status="stopped",
            service_port=8090,
        )
        await storage.upsert_tenant_sync_state(user.tenant_id, digest)
        await storage.insert_audit_event(
            tenant_id=user.tenant_id,
            actor="load-users",
            event_type="tenant_disabled",
            payload={"reason": "users_json_status_disabled"},
        )
        log_event(log, "tenant_disabled", tenant_id=user.tenant_id, tenant_slug=user.tenant_slug)
        return

    await storage.upsert_tenant_credentials(
        tenant_id=user.tenant_id,
        phone_e164=user.phone_e164,
        tg_api_id=user.tg_api_id,
        tg_api_hash_encrypted=box.encrypt(user.tg_api_hash),
        tg_session_encrypted=box.encrypt(user.tg_string_session),
        encryption_version="fernet-v1",
    )

    runtime_spec = build_runtime_spec(
        config=cfg,
        container_name=container_name,
        network_alias=network_alias,
        session=user.tg_string_session,
        tg_api_id=user.tg_api_id,
        tg_api_hash=user.tg_api_hash,
    )
    _, runtime_status = ensure_tenant_container(runtime_spec)
    ready = check_container_ready(container_name, timeout_seconds=35)
    runtime_status = _finalize_runtime_status(runtime_status, ready)

    await storage.upsert_tenant_runtime(
        tenant_id=user.tenant_id,
        container_name=container_name,
        endpoint_base_url=endpoint_base_url,
        runtime_status=runtime_status,
        service_port=8090,
    )
    await storage.bind_bale_user(user.tenant_id, user.bale_user_id, user.bale_chat_id)
    await storage.set_tenant_status(user.tenant_id, "active")
    await storage.upsert_tenant_sync_state(user.tenant_id, digest)
    activated_now = await _mark_registration_as_provisioned(storage, user)
    if activated_now:
        await _notify_user_activation(cfg=cfg, user=user, log=log)

    await storage.insert_audit_event(
        tenant_id=user.tenant_id,
        actor="load-users",
        event_type="tenant_synced",
        payload={"changed": changed, "container_name": container_name, "runtime_status": runtime_status},
    )
    log_event(
        log,
        "tenant_synced",
        tenant_id=user.tenant_id,
        tenant_slug=user.tenant_slug,
        changed=changed,
        runtime_status=runtime_status,
    )


async def _disable_removed_tenant(storage: Storage, tenant_id: str, row: dict[str, object], log) -> None:
    slug = str(row.get("tenant_slug") or tenant_id)
    container_name = str(row.get("container_name") or build_runtime_names(slug)[0])
    removed = stop_and_remove_tenant_container(container_name)
    already_disabled = str(row.get("status") or "") == "disabled"
    already_stopped = str(row.get("runtime_status") or "") == "stopped"
    if already_disabled and already_stopped and not removed:
        return
    await storage.deactivate_bindings_for_tenant(tenant_id)
    await storage.set_tenant_status(tenant_id, "disabled")
    await storage.delete_tenant_sync_state(tenant_id)
    await storage.insert_audit_event(
        tenant_id=tenant_id,
        actor="load-users",
        event_type="tenant_removed_from_config",
        payload={"container_name": container_name},
    )
    log_event(log, "tenant_removed", tenant_id=tenant_id, tenant_slug=slug)


async def _mark_registration_as_provisioned(storage: Storage, user: DesiredUser) -> bool:
    reg = await storage.get_registration_request(user.bale_user_id, user.bale_chat_id)
    if reg is None:
        return False
    if reg.status == RegistrationStatus.PROVISIONED and reg.tenant_id == user.tenant_id:
        return False
    reg.status = RegistrationStatus.PROVISIONED
    reg.tenant_id = user.tenant_id
    reg.note = "Provisioned by load-users"
    await storage.upsert_registration_request(reg)
    return True


async def _notify_user_activation(*, cfg: AdminConfig, user: DesiredUser, log) -> None:
    if not cfg.notify_user_activation:
        log_event(log, "activation_notify_skipped", tenant_id=user.tenant_id, reason="disabled_by_env")
        return

    if not cfg.bale_bot_token:
        log_event(log, "activation_notify_skipped", tenant_id=user.tenant_id, reason="missing_bale_bot_token")
        return

    try:
        await send_text_message(
            bot_token=cfg.bale_bot_token,
            chat_id=user.bale_chat_id,
            text=ACTIVATION_TEXT,
        )
        log_event(
            log,
            "activation_notify_sent",
            tenant_id=user.tenant_id,
            bale_user_id=user.bale_user_id,
            bale_chat_id=user.bale_chat_id,
        )
    except BaleNotifyError as exc:
        log_event(
            log,
            "activation_notify_failed",
            tenant_id=user.tenant_id,
            bale_user_id=user.bale_user_id,
            bale_chat_id=user.bale_chat_id,
            error=str(exc),
        )


def _config_digest(cfg: AdminConfig, user: DesiredUser, container_name: str, network_alias: str) -> str:
    payload = {
        "tenant_id": user.tenant_id,
        "tenant_slug": user.tenant_slug,
        "status": user.status,
        "owner_name": user.owner_name,
        "bale_user_id": user.bale_user_id,
        "bale_chat_id": user.bale_chat_id,
        "phone_e164": user.phone_e164,
        "tg_api_id": user.tg_api_id,
        "tg_api_hash": user.tg_api_hash,
        "tg_string_session": user.tg_string_session,
        "container_name": container_name,
        "network_alias": network_alias,
        "docker_network_name": cfg.docker_network_name,
        "tenant_image": cfg.tenant_container_image,
        "mira_username": cfg.mira_username,
        "tg_proxy_enabled": cfg.tg_proxy_enabled,
        "tg_proxy_type": cfg.tg_proxy_type,
        "tg_proxy_host": cfg.tg_proxy_host,
        "tg_proxy_host_runtime": cfg.tg_proxy_host_runtime,
        "tg_proxy_host_admin": cfg.tg_proxy_host_admin,
        "tg_proxy_port": cfg.tg_proxy_port,
        "tg_proxy_username": cfg.tg_proxy_username,
        "tg_proxy_password": cfg.tg_proxy_password,
        "tg_proxy_rdns": cfg.tg_proxy_rdns,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _slug_to_id(slug: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "", slug.lower()) or "user"
    return base[:24]


if __name__ == "__main__":
    main()
