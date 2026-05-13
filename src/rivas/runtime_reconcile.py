from __future__ import annotations

import argparse
import asyncio
import sys

from .admin import _finalize_runtime_status, build_runtime_spec
from .config import AdminConfig, ConfigError
from .crypto_utils import SecretBox
from .logging_utils import get_logger, log_event, setup_logging
from .storage import Storage
from .tenant_runtime import build_runtime_names, check_container_ready, ensure_tenant_container


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile tenant runtime containers")
    parser.add_argument("--tenant-id", help="Reconcile only one tenant id", default=None)
    args = parser.parse_args()

    try:
        config = AdminConfig.from_env()
    except ConfigError as exc:
        print(f"[config-error] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    setup_logging(config.log_level)
    log = get_logger("rivas.runtime_reconcile")
    asyncio.run(_run(config=config, log=log, tenant_id=args.tenant_id))


async def _run(*, config: AdminConfig, log, tenant_id: str | None) -> None:
    storage = Storage(config.db, retention_days=30)
    await storage.init()
    box = SecretBox(config.master_encryption_key)

    try:
        tenants = await storage.list_tenants()
        rows = [row for row in tenants if str(row.get("status") or "") == "active"]
        if tenant_id:
            rows = [row for row in rows if str(row.get("id") or "") == tenant_id]

        if not rows:
            print("No active tenants found for reconcile.")
            return

        for row in rows:
            tid = str(row.get("id") or "")
            slug = str(row.get("tenant_slug") or "")
            if not tid or not slug:
                continue

            creds = await storage.get_tenant_credentials(tid)
            if not creds:
                print(f"[skip] tenant={tid} has no credentials")
                continue

            try:
                tg_api_id = int(creds.get("tg_api_id"))
                tg_api_hash = box.decrypt(str(creds.get("tg_api_hash_encrypted") or ""))
                tg_session = box.decrypt(str(creds.get("tg_session_encrypted") or ""))
            except Exception as exc:
                log_event(log, "runtime_reconcile_decrypt_failed", tenant_id=tid, error=str(exc))
                print(f"[error] tenant={tid} decrypt failed: {exc}")
                continue

            container_name, network_alias, endpoint_base_url = build_runtime_names(slug)
            spec = build_runtime_spec(
                config=config,
                container_name=container_name,
                network_alias=network_alias,
                session=tg_session,
                tg_api_id=tg_api_id,
                tg_api_hash=tg_api_hash,
            )

            try:
                changed, initial_status = ensure_tenant_container(spec)
                ready = check_container_ready(container_name, timeout_seconds=40)
                runtime_status = _finalize_runtime_status(initial_status, ready)
                await storage.upsert_tenant_runtime(
                    tenant_id=tid,
                    container_name=container_name,
                    endpoint_base_url=endpoint_base_url,
                    runtime_status=runtime_status,
                )
                log_event(
                    log,
                    "runtime_reconcile_ok",
                    tenant_id=tid,
                    tenant_slug=slug,
                    changed=changed,
                    runtime_status=runtime_status,
                )
                print(f"[ok] tenant={tid} slug={slug} changed={changed} status={runtime_status}")
            except Exception as exc:
                await storage.upsert_tenant_runtime(
                    tenant_id=tid,
                    container_name=container_name,
                    endpoint_base_url=endpoint_base_url,
                    runtime_status="error",
                    last_error=str(exc),
                )
                log_event(log, "runtime_reconcile_failed", tenant_id=tid, tenant_slug=slug, error=str(exc))
                print(f"[error] tenant={tid} slug={slug}: {exc}")
    finally:
        await storage.close()


if __name__ == "__main__":
    main()
