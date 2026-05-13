from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from datetime import datetime

from .admin import _collect_telethon_session, _finalize_runtime_status, _validate_existing_session, build_runtime_spec
from .bale_notifier import BaleNotifyError, send_text_message
from .config import AdminConfig, ConfigError
from .crypto_utils import SecretBox
from .logging_utils import get_logger, log_event, setup_logging
from .models import RegistrationStatus
from .storage import Storage
from .tenant_runtime import build_runtime_names, check_container_ready, ensure_tenant_container

ACTIVATION_TEXT = (
    "ثبت‌نامت با موفقیت فعال شد.\n"
    "از الان می‌تونی با ریواس کار کنی.\n"
    "برای شروع، /start رو بزن."
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive provisioning for pending Bale registrations")
    parser.add_argument("--once", action="store_true", help="Handle at most one selected request and exit")
    args = parser.parse_args()

    try:
        config = AdminConfig.from_env()
    except ConfigError as exc:
        print(f"[config-error] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    setup_logging(config.log_level)
    log = get_logger("rivas.registration_admin")
    asyncio.run(_run(config, log, once=args.once))


async def _run(config: AdminConfig, log, *, once: bool) -> None:
    storage = Storage(config.db, retention_days=30)
    await storage.init()
    try:
        while True:
            pending = await storage.list_pending_registration_requests()
            if not pending:
                print("No pending registration requests.")
                return

            _print_pending_rows(pending)
            choice = input("Select request number (`r` refresh, `q` quit): ").strip().lower()
            if choice in {"q", "quit", "exit"}:
                return
            if choice in {"r", "refresh", ""}:
                continue
            if not choice.isdigit():
                print("Invalid choice. Please enter a number.")
                continue

            idx = int(choice)
            if idx < 1 or idx > len(pending):
                print(f"Invalid number. Enter between 1 and {len(pending)}.")
                continue

            row = pending[idx - 1]
            status = str(row.get("status") or "").strip()
            if status != RegistrationStatus.PENDING_ADMIN.value:
                print(
                    f"Request status is `{status}` and is not ready for provisioning yet. "
                    "User must complete registration first."
                )
                if once:
                    return
                continue

            await _provision_selected(config=config, storage=storage, log=log, row=row)
            if once:
                return
    finally:
        await storage.close()


def _print_pending_rows(rows: list[dict[str, object]]) -> None:
    print("")
    print("Pending Registration Requests:")
    print("-" * 92)
    for i, row in enumerate(rows, start=1):
        updated_ts = int(row.get("updated_ts") or 0)
        updated_at = _format_ts(updated_ts)
        print(
            f"[{i}] status={row.get('status')} user={row.get('desired_username') or '-'} "
            f"phone={row.get('phone_e164') or '-'} "
            f"bale_user_id={row.get('bale_user_id')} chat_id={row.get('bale_chat_id')} updated={updated_at}"
        )
    print("-" * 92)


async def _provision_selected(*, config: AdminConfig, storage: Storage, log, row: dict[str, object]) -> None:
    bale_user_id = str(row.get("bale_user_id") or "").strip()
    bale_chat_id = str(row.get("bale_chat_id") or "").strip()
    desired_username = str(row.get("desired_username") or "").strip()
    default_phone = str(row.get("phone_e164") or "").strip()

    tenant_slug_default = _suggest_tenant_slug(desired_username, bale_user_id)
    owner_name_default = desired_username or tenant_slug_default
    tenant_id_default = f"tenant_{uuid.uuid4().hex[:14]}"

    print("")
    print("Enter provisioning details (press Enter to accept defaults).")

    tenant_slug = _prompt_nonempty("tenant_slug", tenant_slug_default)
    owner_name = _prompt_nonempty("owner_name", owner_name_default)
    phone = _prompt_nonempty("phone_e164", default_phone)
    tg_api_id = _prompt_int("telegram api_id", config.tg_api_id)
    tg_api_hash = _prompt_nonempty("telegram api_hash", config.tg_api_hash)
    tenant_id = _prompt_nonempty("tenant_id", tenant_id_default)

    existing_session = input("Existing string_session (optional, Enter to use OTP flow): ").strip()

    print("")
    print("Provisioning summary:")
    print(f"- tenant_id: {tenant_id}")
    print(f"- tenant_slug: {tenant_slug}")
    print(f"- owner_name: {owner_name}")
    print(f"- bale_user_id: {bale_user_id}")
    print(f"- bale_chat_id: {bale_chat_id}")
    print(f"- phone_e164: {phone}")
    print(f"- tg_api_id: {tg_api_id}")
    print(f"- session_mode: {'provided' if existing_session else 'otp'}")
    confirm = input("Proceed? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("Cancelled.")
        return

    existing = await storage.find_active_tenant_by_phone(phone, tg_api_id)
    if existing and str(existing.get("tenant_id") or "") != tenant_id:
        raise RuntimeError(
            "This Telegram account is already linked to another active tenant: "
            f"{existing.get('tenant_id')}"
        )

    if existing_session:
        tg_string_session = await _validate_existing_session(
            tg_api_id=tg_api_id,
            tg_api_hash=tg_api_hash,
            session=existing_session,
            config=config,
        )
    else:
        tg_string_session = await _collect_telethon_session(
            phone=phone,
            tg_api_id=tg_api_id,
            tg_api_hash=tg_api_hash,
            config=config,
        )

    box = SecretBox(config.master_encryption_key)
    await storage.upsert_tenant(tenant_id=tenant_id, tenant_slug=tenant_slug, owner_name=owner_name, status="active")
    await storage.upsert_tenant_credentials(
        tenant_id=tenant_id,
        phone_e164=phone,
        tg_api_id=tg_api_id,
        tg_api_hash_encrypted=box.encrypt(tg_api_hash),
        tg_session_encrypted=box.encrypt(tg_string_session),
        encryption_version="fernet-v1",
    )

    container_name, network_alias, endpoint_base_url = build_runtime_names(tenant_slug)
    runtime_spec = build_runtime_spec(
        config=config,
        container_name=container_name,
        network_alias=network_alias,
        session=tg_string_session,
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
    await storage.bind_bale_user(tenant_id=tenant_id, bale_user_id=bale_user_id, bale_chat_id=bale_chat_id)
    await storage.set_tenant_status(tenant_id, "active")
    await _set_registration_provisioned(storage, bale_user_id, bale_chat_id, tenant_id)

    await storage.insert_audit_event(
        tenant_id=tenant_id,
        actor="registration-admin",
        event_type="registration_provisioned_interactive",
        payload={
            "tenant_id": tenant_id,
            "tenant_slug": tenant_slug,
            "owner_name": owner_name,
            "bale_user_id": bale_user_id,
            "bale_chat_id": bale_chat_id,
            "runtime_status": runtime_status,
        },
    )

    await _notify_user_activation(config=config, bale_chat_id=bale_chat_id, tenant_id=tenant_id, log=log)
    log_event(
        log,
        "registration_provisioned_interactive",
        tenant_id=tenant_id,
        bale_user_id=bale_user_id,
        bale_chat_id=bale_chat_id,
        runtime_status=runtime_status,
    )

    print("")
    print("Provisioning completed.")
    print(f"- tenant_id: {tenant_id}")
    print(f"- container_name: {container_name}")
    print(f"- endpoint_base_url: {endpoint_base_url}")
    print(f"- runtime_status: {runtime_status}")


async def _set_registration_provisioned(storage: Storage, bale_user_id: str, bale_chat_id: str, tenant_id: str) -> None:
    reg = await storage.get_registration_request(bale_user_id, bale_chat_id)
    if reg is None:
        return
    reg.status = RegistrationStatus.PROVISIONED
    reg.tenant_id = tenant_id
    reg.note = "Provisioned by interactive registration admin"
    await storage.upsert_registration_request(reg)


async def _notify_user_activation(*, config: AdminConfig, bale_chat_id: str, tenant_id: str, log) -> None:
    if not config.notify_user_activation:
        log_event(log, "activation_notify_skipped", tenant_id=tenant_id, reason="disabled_by_env")
        return
    if not config.bale_bot_token:
        log_event(log, "activation_notify_skipped", tenant_id=tenant_id, reason="missing_bale_bot_token")
        return
    try:
        await send_text_message(
            bot_token=config.bale_bot_token,
            chat_id=bale_chat_id,
            text=ACTIVATION_TEXT,
        )
        log_event(log, "activation_notify_sent", tenant_id=tenant_id, bale_chat_id=bale_chat_id)
    except BaleNotifyError as exc:
        log_event(log, "activation_notify_failed", tenant_id=tenant_id, bale_chat_id=bale_chat_id, error=str(exc))


def _suggest_tenant_slug(desired_username: str, bale_user_id: str) -> str:
    source = desired_username.strip().lower() if desired_username else ""
    source = re.sub(r"[^a-z0-9]+", "-", source)
    source = source.strip("-")
    if not source:
        source = f"user-{bale_user_id}"
    return source[:42]


def _prompt_nonempty(label: str, default_value: str) -> str:
    default_value = default_value.strip()
    while True:
        raw = input(f"{label} [{default_value}]: ").strip()
        value = raw or default_value
        if value:
            return value
        print(f"{label} cannot be empty.")


def _prompt_int(label: str, default_value: int) -> int:
    while True:
        raw = input(f"{label} [{default_value}]: ").strip()
        if not raw:
            return int(default_value)
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a valid integer.")
            continue
        if value <= 0:
            print("Value must be positive.")
            continue
        return value


def _format_ts(ts: int) -> str:
    if ts <= 0:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    main()
