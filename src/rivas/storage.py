from __future__ import annotations

import json
import time
from pathlib import Path

import aiomysql
import aiosqlite

from .config import DatabaseConfig
from .models import MiraResponse, RegistrationRequest, RegistrationStatus, RequestPayload, RequestStatus, TenantBinding


class Storage:
    def __init__(self, db_config: DatabaseConfig, retention_days: int) -> None:
        self._db = db_config
        self._retention_days = retention_days
        self._sqlite_conn: aiosqlite.Connection | None = None
        self._mysql_pool: aiomysql.Pool | None = None

    async def init(self) -> None:
        if self._db.backend == "sqlite":
            await self._init_sqlite()
            return
        await self._init_mysql()

    async def close(self) -> None:
        if self._sqlite_conn is not None:
            await self._sqlite_conn.close()
            self._sqlite_conn = None
        if self._mysql_pool is not None:
            self._mysql_pool.close()
            await self._mysql_pool.wait_closed()
            self._mysql_pool = None

    async def create_request(self, payload: RequestPayload) -> None:
        now = _now_ts()
        query = (
            """
            INSERT INTO requests (
                request_id, tenant_id, bale_user_id, bale_chat_id, input_type,
                input_text, input_caption, input_file_name, input_file_size, input_mime_type,
                queue_entered_ts, status, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            if self._is_sqlite()
            else
            """
            INSERT INTO requests (
                request_id, tenant_id, bale_user_id, bale_chat_id, input_type,
                input_text, input_caption, input_file_name, input_file_size, input_mime_type,
                queue_entered_ts, status, created_ts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        )
        args = (
            payload.request_id,
            payload.tenant_id,
            payload.bale_user_id,
            payload.bale_chat_id,
            payload.input_type.value,
            payload.text,
            payload.caption,
            payload.file_name,
            payload.file_size,
            payload.mime_type,
            now,
            RequestStatus.QUEUED.value,
            now,
        )
        await self._execute(query, args)

    async def mark_processing(self, request_id: str) -> None:
        query = "UPDATE requests SET status = ?, mira_sent_ts = ? WHERE request_id = ?" if self._is_sqlite() else "UPDATE requests SET status = %s, mira_sent_ts = %s WHERE request_id = %s"
        await self._execute(query, (RequestStatus.PROCESSING.value, _now_ts(), request_id))

    async def mark_failed(self, request_id: str, error_code: str, error_message: str) -> None:
        query = (
            "UPDATE requests SET status = ?, error_code = ?, error_message = ?, completed_ts = ? WHERE request_id = ?"
            if self._is_sqlite()
            else
            "UPDATE requests SET status = %s, error_code = %s, error_message = %s, completed_ts = %s WHERE request_id = %s"
        )
        await self._execute(query, (RequestStatus.FAILED.value, error_code, error_message, _now_ts(), request_id))

    async def mark_completed(self, request_id: str, response: MiraResponse) -> None:
        query = (
            """
            UPDATE requests
            SET status = ?, completed_ts = ?, output_text = ?, output_media_count = ?, output_payload_json = ?
            WHERE request_id = ?
            """
            if self._is_sqlite()
            else
            """
            UPDATE requests
            SET status = %s, completed_ts = %s, output_text = %s, output_media_count = %s, output_payload_json = %s
            WHERE request_id = %s
            """
        )
        await self._execute(
            query,
            (
                RequestStatus.COMPLETED.value,
                _now_ts(),
                response.text,
                len(response.media_parts),
                json.dumps(response.as_json(), ensure_ascii=False),
                request_id,
            ),
        )

    async def set_user_mode(self, user_id: str, mode: str) -> None:
        now = _now_ts()
        if self._is_sqlite():
            query = (
                """
                INSERT INTO user_settings (user_id, mode, tts_enabled, updated_ts)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET mode = excluded.mode, updated_ts = excluded.updated_ts
                """
            )
            args = (user_id, mode, now)
        else:
            query = (
                """
                INSERT INTO user_settings (user_id, mode, tts_enabled, updated_ts)
                VALUES (%s, %s, 0, %s) AS new
                ON DUPLICATE KEY UPDATE mode = new.mode, updated_ts = new.updated_ts
                """
            )
            args = (user_id, mode, now)
        await self._execute(query, args)

    async def fail_stale_active_requests(self, stale_seconds: int) -> int:
        cutoff = _now_ts() - max(1, int(stale_seconds))
        if self._is_sqlite():
            count_query = (
                "SELECT COUNT(1) AS c FROM requests WHERE status IN (?, ?) AND queue_entered_ts < ?"
            )
            update_query = (
                "UPDATE requests SET status = ?, error_code = ?, error_message = ?, completed_ts = ? "
                "WHERE status IN (?, ?) AND queue_entered_ts < ?"
            )
            count_row = await self._fetchone(count_query, (RequestStatus.QUEUED.value, RequestStatus.PROCESSING.value, cutoff))
            count = int((count_row or {}).get("c") or 0)
            if count == 0:
                return 0
            await self._execute(
                update_query,
                (
                    RequestStatus.FAILED.value,
                    "stale_runtime_recovery",
                    "Request was stale after runtime restart/recovery.",
                    _now_ts(),
                    RequestStatus.QUEUED.value,
                    RequestStatus.PROCESSING.value,
                    cutoff,
                ),
            )
            return count

        count_query = (
            "SELECT COUNT(1) AS c FROM requests WHERE status IN (%s, %s) AND queue_entered_ts < %s"
        )
        update_query = (
            "UPDATE requests SET status = %s, error_code = %s, error_message = %s, completed_ts = %s "
            "WHERE status IN (%s, %s) AND queue_entered_ts < %s"
        )
        count_row = await self._fetchone(count_query, (RequestStatus.QUEUED.value, RequestStatus.PROCESSING.value, cutoff))
        count = int((count_row or {}).get("c") or 0)
        if count == 0:
            return 0
        await self._execute(
            update_query,
            (
                RequestStatus.FAILED.value,
                "stale_runtime_recovery",
                "Request was stale after runtime restart/recovery.",
                _now_ts(),
                RequestStatus.QUEUED.value,
                RequestStatus.PROCESSING.value,
                cutoff,
            ),
        )
        return count

    async def set_user_tts(self, user_id: str, enabled: bool) -> None:
        now = _now_ts()
        if self._is_sqlite():
            query = (
                """
                INSERT INTO user_settings (user_id, mode, tts_enabled, updated_ts)
                VALUES (?, 'chat', ?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET tts_enabled = excluded.tts_enabled, updated_ts = excluded.updated_ts
                """
            )
            args = (user_id, int(enabled), now)
        else:
            query = (
                """
                INSERT INTO user_settings (user_id, mode, tts_enabled, updated_ts)
                VALUES (%s, 'chat', %s, %s) AS new
                ON DUPLICATE KEY UPDATE tts_enabled = new.tts_enabled, updated_ts = new.updated_ts
                """
            )
            args = (user_id, int(enabled), now)
        await self._execute(query, args)

    async def get_user_settings(self, user_id: str) -> dict[str, object]:
        query = "SELECT mode, tts_enabled FROM user_settings WHERE user_id = ?" if self._is_sqlite() else "SELECT mode, tts_enabled FROM user_settings WHERE user_id = %s"
        row = await self._fetchone(query, (user_id,))
        if row is None:
            return {"mode": "chat", "tts_enabled": False}
        return {
            "mode": row.get("mode") or "chat",
            "tts_enabled": bool(row.get("tts_enabled")),
        }

    async def cleanup_expired(self) -> int:
        threshold = _now_ts() - self._retention_days * 24 * 60 * 60
        query = "DELETE FROM requests WHERE created_ts < ?" if self._is_sqlite() else "DELETE FROM requests WHERE created_ts < %s"
        return await self._execute(query, (threshold,), return_rowcount=True)

    async def pending_count(self) -> int:
        query = (
            "SELECT COUNT(1) AS c FROM requests WHERE status IN (?, ?)"
            if self._is_sqlite()
            else
            "SELECT COUNT(1) AS c FROM requests WHERE status IN (%s, %s)"
        )
        row = await self._fetchone(query, (RequestStatus.QUEUED.value, RequestStatus.PROCESSING.value))
        return int((row or {}).get("c") or 0)

    async def get_request_snapshot(self, request_id: str) -> dict[str, object] | None:
        query = "SELECT * FROM requests WHERE request_id = ?" if self._is_sqlite() else "SELECT * FROM requests WHERE request_id = %s"
        return await self._fetchone(query, (request_id,))

    async def upsert_tenant(self, tenant_id: str, tenant_slug: str, owner_name: str, status: str = "active") -> None:
        now = _now_ts()
        if self._is_sqlite():
            query = (
                """
                INSERT INTO tenants (id, tenant_slug, owner_name, status, created_ts, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id)
                DO UPDATE SET tenant_slug = excluded.tenant_slug, owner_name = excluded.owner_name, status = excluded.status, updated_ts = excluded.updated_ts
                """
            )
            args = (tenant_id, tenant_slug, owner_name, status, now, now)
        else:
            query = (
                """
                INSERT INTO tenants (id, tenant_slug, owner_name, status, created_ts, updated_ts)
                VALUES (%s, %s, %s, %s, %s, %s) AS new
                ON DUPLICATE KEY UPDATE tenant_slug = new.tenant_slug, owner_name = new.owner_name, status = new.status, updated_ts = new.updated_ts
                """
            )
            args = (tenant_id, tenant_slug, owner_name, status, now, now)
        await self._execute(query, args)

    async def upsert_tenant_credentials(
        self,
        tenant_id: str,
        phone_e164: str,
        tg_api_id: int,
        tg_api_hash_encrypted: str,
        tg_session_encrypted: str,
        encryption_version: str = "fernet-v1",
    ) -> None:
        now = _now_ts()
        if self._is_sqlite():
            query = (
                """
                INSERT INTO tenant_credentials (
                    tenant_id, phone_e164, tg_api_id, tg_api_hash_encrypted, tg_session_encrypted, encryption_version, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id)
                DO UPDATE SET
                    phone_e164 = excluded.phone_e164,
                    tg_api_id = excluded.tg_api_id,
                    tg_api_hash_encrypted = excluded.tg_api_hash_encrypted,
                    tg_session_encrypted = excluded.tg_session_encrypted,
                    encryption_version = excluded.encryption_version,
                    updated_ts = excluded.updated_ts
                """
            )
            args = (tenant_id, phone_e164, tg_api_id, tg_api_hash_encrypted, tg_session_encrypted, encryption_version, now)
        else:
            query = (
                """
                INSERT INTO tenant_credentials (
                    tenant_id, phone_e164, tg_api_id, tg_api_hash_encrypted, tg_session_encrypted, encryption_version, updated_ts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s) AS new
                ON DUPLICATE KEY UPDATE
                    phone_e164 = new.phone_e164,
                    tg_api_id = new.tg_api_id,
                    tg_api_hash_encrypted = new.tg_api_hash_encrypted,
                    tg_session_encrypted = new.tg_session_encrypted,
                    encryption_version = new.encryption_version,
                    updated_ts = new.updated_ts
                """
            )
            args = (tenant_id, phone_e164, tg_api_id, tg_api_hash_encrypted, tg_session_encrypted, encryption_version, now)
        await self._execute(query, args)

    async def upsert_tenant_runtime(
        self,
        tenant_id: str,
        container_name: str,
        endpoint_base_url: str,
        runtime_status: str,
        service_port: int = 8090,
        last_error: str | None = None,
    ) -> None:
        now = _now_ts()
        if self._is_sqlite():
            query = (
                """
                INSERT INTO tenant_runtime (
                    tenant_id, container_name, endpoint_base_url, runtime_status, service_port, last_health_ts, last_error, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id)
                DO UPDATE SET
                    container_name = excluded.container_name,
                    endpoint_base_url = excluded.endpoint_base_url,
                    runtime_status = excluded.runtime_status,
                    service_port = excluded.service_port,
                    last_health_ts = excluded.last_health_ts,
                    last_error = excluded.last_error,
                    updated_ts = excluded.updated_ts
                """
            )
            args = (tenant_id, container_name, endpoint_base_url, runtime_status, service_port, now, last_error, now)
        else:
            query = (
                """
                INSERT INTO tenant_runtime (
                    tenant_id, container_name, endpoint_base_url, runtime_status, service_port, last_health_ts, last_error, updated_ts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) AS new
                ON DUPLICATE KEY UPDATE
                    container_name = new.container_name,
                    endpoint_base_url = new.endpoint_base_url,
                    runtime_status = new.runtime_status,
                    service_port = new.service_port,
                    last_health_ts = new.last_health_ts,
                    last_error = new.last_error,
                    updated_ts = new.updated_ts
                """
            )
            args = (tenant_id, container_name, endpoint_base_url, runtime_status, service_port, now, last_error, now)
        await self._execute(query, args)

    async def bind_bale_user(self, tenant_id: str, bale_user_id: str, bale_chat_id: str) -> None:
        now = _now_ts()
        if self._is_sqlite():
            query = (
                """
                INSERT INTO bale_bindings (bale_user_id, bale_chat_id, tenant_id, is_active, bound_ts, updated_ts)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(bale_user_id, bale_chat_id)
                DO UPDATE SET tenant_id = excluded.tenant_id, is_active = excluded.is_active, updated_ts = excluded.updated_ts
                """
            )
            args = (bale_user_id, bale_chat_id, tenant_id, now, now)
        else:
            query = (
                """
                INSERT INTO bale_bindings (bale_user_id, bale_chat_id, tenant_id, is_active, bound_ts, updated_ts)
                VALUES (%s, %s, %s, 1, %s, %s) AS new
                ON DUPLICATE KEY UPDATE tenant_id = new.tenant_id, is_active = new.is_active, updated_ts = new.updated_ts
                """
            )
            args = (bale_user_id, bale_chat_id, tenant_id, now, now)
        await self._execute(query, args)

    async def resolve_tenant_binding(self, bale_user_id: str, bale_chat_id: str) -> TenantBinding | None:
        query = (
            """
            SELECT
                b.tenant_id AS tenant_id,
                t.tenant_slug AS tenant_slug,
                b.bale_user_id AS bale_user_id,
                b.bale_chat_id AS bale_chat_id,
                t.status AS tenant_status,
                r.endpoint_base_url AS endpoint_base_url,
                r.runtime_status AS runtime_status
            FROM bale_bindings b
            INNER JOIN tenants t ON t.id = b.tenant_id
            LEFT JOIN tenant_runtime r ON r.tenant_id = b.tenant_id
            WHERE b.bale_user_id = ? AND b.bale_chat_id = ? AND b.is_active = 1
            """
            if self._is_sqlite()
            else
            """
            SELECT
                b.tenant_id AS tenant_id,
                t.tenant_slug AS tenant_slug,
                b.bale_user_id AS bale_user_id,
                b.bale_chat_id AS bale_chat_id,
                t.status AS tenant_status,
                r.endpoint_base_url AS endpoint_base_url,
                r.runtime_status AS runtime_status
            FROM bale_bindings b
            INNER JOIN tenants t ON t.id = b.tenant_id
            LEFT JOIN tenant_runtime r ON r.tenant_id = b.tenant_id
            WHERE b.bale_user_id = %s AND b.bale_chat_id = %s AND b.is_active = 1
            """
        )
        row = await self._fetchone(query, (bale_user_id, bale_chat_id))
        if row is None:
            return None

        return TenantBinding(
            tenant_id=str(row.get("tenant_id") or ""),
            tenant_slug=str(row.get("tenant_slug") or ""),
            bale_user_id=str(row.get("bale_user_id") or ""),
            bale_chat_id=str(row.get("bale_chat_id") or ""),
            tenant_status=str(row.get("tenant_status") or "unknown"),
            endpoint_base_url=str(row.get("endpoint_base_url") or ""),
            runtime_status=str(row.get("runtime_status") or "unknown"),
        )

    async def list_tenants(self) -> list[dict[str, object]]:
        query = (
            """
            SELECT t.id, t.tenant_slug, t.owner_name, t.status, r.container_name, r.endpoint_base_url, r.runtime_status
            FROM tenants t
            LEFT JOIN tenant_runtime r ON r.tenant_id = t.id
            ORDER BY t.created_ts DESC
            """
        )
        return await self._fetchall(query, ())

    async def list_bale_bindings(self) -> list[dict[str, object]]:
        query = (
            "SELECT bale_user_id, bale_chat_id, tenant_id, is_active FROM bale_bindings"
        )
        return await self._fetchall(query, ())

    async def deactivate_bindings_for_tenant(self, tenant_id: str) -> None:
        query = (
            "UPDATE bale_bindings SET is_active = 0, updated_ts = ? WHERE tenant_id = ?"
            if self._is_sqlite()
            else
            "UPDATE bale_bindings SET is_active = %s, updated_ts = %s WHERE tenant_id = %s"
        )
        if self._is_sqlite():
            await self._execute(query, (_now_ts(), tenant_id))
            return
        await self._execute(query, (0, _now_ts(), tenant_id))

    async def get_tenant_credentials(self, tenant_id: str) -> dict[str, object] | None:
        query = "SELECT * FROM tenant_credentials WHERE tenant_id = ?" if self._is_sqlite() else "SELECT * FROM tenant_credentials WHERE tenant_id = %s"
        return await self._fetchone(query, (tenant_id,))

    async def find_active_tenant_by_phone(self, phone_e164: str, tg_api_id: int) -> dict[str, object] | None:
        query = (
            """
            SELECT t.id AS tenant_id, t.tenant_slug, t.status, r.runtime_status
            FROM tenant_credentials c
            INNER JOIN tenants t ON t.id = c.tenant_id
            LEFT JOIN tenant_runtime r ON r.tenant_id = c.tenant_id
            WHERE c.phone_e164 = ? AND c.tg_api_id = ?
            ORDER BY t.created_ts DESC
            LIMIT 1
            """
            if self._is_sqlite()
            else
            """
            SELECT t.id AS tenant_id, t.tenant_slug, t.status, r.runtime_status
            FROM tenant_credentials c
            INNER JOIN tenants t ON t.id = c.tenant_id
            LEFT JOIN tenant_runtime r ON r.tenant_id = c.tenant_id
            WHERE c.phone_e164 = %s AND c.tg_api_id = %s
            ORDER BY t.created_ts DESC
            LIMIT 1
            """
        )
        return await self._fetchone(query, (phone_e164, tg_api_id))

    async def set_tenant_status(self, tenant_id: str, status: str) -> None:
        query = "UPDATE tenants SET status = ?, updated_ts = ? WHERE id = ?" if self._is_sqlite() else "UPDATE tenants SET status = %s, updated_ts = %s WHERE id = %s"
        await self._execute(query, (status, _now_ts(), tenant_id))
        if status != "active":
            runtime_query = (
                "UPDATE tenant_runtime SET runtime_status = ?, updated_ts = ? WHERE tenant_id = ?"
                if self._is_sqlite()
                else
                "UPDATE tenant_runtime SET runtime_status = %s, updated_ts = %s WHERE tenant_id = %s"
            )
            await self._execute(runtime_query, ("stopped", _now_ts(), tenant_id))

    async def get_tenant_sync_state(self, tenant_id: str) -> str | None:
        query = (
            "SELECT config_digest FROM tenant_sync_state WHERE tenant_id = ?"
            if self._is_sqlite()
            else
            "SELECT config_digest FROM tenant_sync_state WHERE tenant_id = %s"
        )
        row = await self._fetchone(query, (tenant_id,))
        if row is None:
            return None
        return str(row.get("config_digest") or "")

    async def upsert_tenant_sync_state(self, tenant_id: str, config_digest: str) -> None:
        now = _now_ts()
        if self._is_sqlite():
            query = (
                """
                INSERT INTO tenant_sync_state (tenant_id, config_digest, updated_ts)
                VALUES (?, ?, ?)
                ON CONFLICT(tenant_id)
                DO UPDATE SET config_digest = excluded.config_digest, updated_ts = excluded.updated_ts
                """
            )
            await self._execute(query, (tenant_id, config_digest, now))
            return
        query = (
            """
            INSERT INTO tenant_sync_state (tenant_id, config_digest, updated_ts)
            VALUES (%s, %s, %s) AS new
            ON DUPLICATE KEY UPDATE config_digest = new.config_digest, updated_ts = new.updated_ts
            """
        )
        await self._execute(query, (tenant_id, config_digest, now))

    async def delete_tenant_sync_state(self, tenant_id: str) -> None:
        query = "DELETE FROM tenant_sync_state WHERE tenant_id = ?" if self._is_sqlite() else "DELETE FROM tenant_sync_state WHERE tenant_id = %s"
        await self._execute(query, (tenant_id,))

    async def get_registration_request(self, bale_user_id: str, bale_chat_id: str) -> RegistrationRequest | None:
        query = (
            """
            SELECT bale_user_id, bale_chat_id, status, desired_username, phone_e164, note, tenant_id
            FROM registration_requests
            WHERE bale_user_id = ? AND bale_chat_id = ?
            """
            if self._is_sqlite()
            else
            """
            SELECT bale_user_id, bale_chat_id, status, desired_username, phone_e164, note, tenant_id
            FROM registration_requests
            WHERE bale_user_id = %s AND bale_chat_id = %s
            """
        )
        row = await self._fetchone(query, (bale_user_id, bale_chat_id))
        if row is None:
            return None
        status_raw = str(row.get("status") or RegistrationStatus.AWAITING_USERNAME.value)
        try:
            status = RegistrationStatus(status_raw)
        except ValueError:
            status = RegistrationStatus.AWAITING_USERNAME
        return RegistrationRequest(
            bale_user_id=str(row.get("bale_user_id") or bale_user_id),
            bale_chat_id=str(row.get("bale_chat_id") or bale_chat_id),
            status=status,
            desired_username=str(row.get("desired_username")) if row.get("desired_username") is not None else None,
            phone_e164=str(row.get("phone_e164")) if row.get("phone_e164") is not None else None,
            note=str(row.get("note")) if row.get("note") is not None else None,
            tenant_id=str(row.get("tenant_id")) if row.get("tenant_id") is not None else None,
        )

    async def upsert_registration_request(self, request: RegistrationRequest) -> None:
        now = _now_ts()
        if self._is_sqlite():
            query = (
                """
                INSERT INTO registration_requests (
                    bale_user_id, bale_chat_id, status, desired_username, phone_e164, note, tenant_id, created_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bale_user_id, bale_chat_id)
                DO UPDATE SET
                    status = excluded.status,
                    desired_username = excluded.desired_username,
                    phone_e164 = excluded.phone_e164,
                    note = excluded.note,
                    tenant_id = excluded.tenant_id,
                    updated_ts = excluded.updated_ts
                """
            )
            args = (
                request.bale_user_id,
                request.bale_chat_id,
                request.status.value,
                request.desired_username,
                request.phone_e164,
                request.note,
                request.tenant_id,
                now,
                now,
            )
            await self._execute(query, args)
            return
        query = (
            """
            INSERT INTO registration_requests (
                bale_user_id, bale_chat_id, status, desired_username, phone_e164, note, tenant_id, created_ts, updated_ts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) AS new
            ON DUPLICATE KEY UPDATE
                status = new.status,
                desired_username = new.desired_username,
                phone_e164 = new.phone_e164,
                note = new.note,
                tenant_id = new.tenant_id,
                updated_ts = new.updated_ts
            """
        )
        args = (
            request.bale_user_id,
            request.bale_chat_id,
            request.status.value,
            request.desired_username,
            request.phone_e164,
            request.note,
            request.tenant_id,
            now,
            now,
        )
        await self._execute(query, args)

    async def list_pending_registration_requests(self) -> list[dict[str, object]]:
        query = (
            """
            SELECT bale_user_id, bale_chat_id, status, desired_username, phone_e164, note, tenant_id, created_ts, updated_ts
            FROM registration_requests
            WHERE status IN (?, ?, ?)
            ORDER BY updated_ts DESC
            """
            if self._is_sqlite()
            else
            """
            SELECT bale_user_id, bale_chat_id, status, desired_username, phone_e164, note, tenant_id, created_ts, updated_ts
            FROM registration_requests
            WHERE status IN (%s, %s, %s)
            ORDER BY updated_ts DESC
            """
        )
        return await self._fetchall(
            query,
            (
                RegistrationStatus.AWAITING_USERNAME.value,
                RegistrationStatus.AWAITING_PHONE.value,
                RegistrationStatus.PENDING_ADMIN.value,
            ),
        )

    async def insert_audit_event(self, tenant_id: str, actor: str, event_type: str, payload: dict[str, object]) -> None:
        query = (
            "INSERT INTO audit_events (tenant_id, actor, event_type, payload_json, created_ts) VALUES (?, ?, ?, ?, ?)"
            if self._is_sqlite()
            else
            "INSERT INTO audit_events (tenant_id, actor, event_type, payload_json, created_ts) VALUES (%s, %s, %s, %s, %s)"
        )
        await self._execute(query, (tenant_id, actor, event_type, json.dumps(payload, ensure_ascii=False), _now_ts()))

    def _is_sqlite(self) -> bool:
        return self._db.backend == "sqlite"

    async def _init_sqlite(self) -> None:
        path = self._db.sqlite_path
        if path is None:
            raise RuntimeError("sqlite_path is missing")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY,
                tenant_slug TEXT NOT NULL UNIQUE,
                owner_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tenant_credentials (
                tenant_id TEXT PRIMARY KEY,
                phone_e164 TEXT NOT NULL,
                tg_api_id INTEGER NOT NULL,
                tg_api_hash_encrypted TEXT NOT NULL,
                tg_session_encrypted TEXT NOT NULL,
                encryption_version TEXT NOT NULL,
                updated_ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tenant_runtime (
                tenant_id TEXT PRIMARY KEY,
                container_name TEXT NOT NULL,
                endpoint_base_url TEXT NOT NULL,
                runtime_status TEXT NOT NULL,
                service_port INTEGER NOT NULL,
                last_health_ts INTEGER,
                last_error TEXT,
                updated_ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bale_bindings (
                bale_user_id TEXT NOT NULL,
                bale_chat_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                bound_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL,
                PRIMARY KEY (bale_user_id, bale_chat_id)
            );

            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
                tenant_id TEXT,
                bale_user_id TEXT NOT NULL,
                bale_chat_id TEXT NOT NULL,
                input_type TEXT NOT NULL,
                input_text TEXT,
                input_caption TEXT,
                input_file_name TEXT,
                input_file_size INTEGER,
                input_mime_type TEXT,
                queue_entered_ts INTEGER NOT NULL,
                mira_sent_ts INTEGER,
                completed_ts INTEGER,
                status TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                output_text TEXT,
                output_media_count INTEGER NOT NULL DEFAULT 0,
                output_payload_json TEXT,
                created_ts INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_requests_created_ts ON requests(created_ts);
            CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
            CREATE INDEX IF NOT EXISTS idx_requests_tenant_id ON requests(tenant_id);

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'chat',
                tts_enabled INTEGER NOT NULL DEFAULT 0,
                updated_ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT,
                created_ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS registration_requests (
                bale_user_id TEXT NOT NULL,
                bale_chat_id TEXT NOT NULL,
                status TEXT NOT NULL,
                desired_username TEXT,
                phone_e164 TEXT,
                note TEXT,
                tenant_id TEXT,
                created_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL,
                PRIMARY KEY (bale_user_id, bale_chat_id)
            );

            CREATE TABLE IF NOT EXISTS tenant_sync_state (
                tenant_id TEXT PRIMARY KEY,
                config_digest TEXT NOT NULL,
                updated_ts INTEGER NOT NULL
            );
            """
        )
        await conn.commit()
        self._sqlite_conn = conn

    async def _init_mysql(self) -> None:
        pool = await aiomysql.create_pool(
            host=self._db.mysql_host,
            port=int(self._db.mysql_port or 3306),
            user=self._db.mysql_user,
            password=self._db.mysql_password,
            db=self._db.mysql_database,
            autocommit=True,
            minsize=1,
            maxsize=10,
            charset="utf8mb4",
        )
        self._mysql_pool = pool

        schema_statements = [
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id VARCHAR(64) PRIMARY KEY,
                tenant_slug VARCHAR(128) NOT NULL UNIQUE,
                owner_name VARCHAR(255) NOT NULL,
                status VARCHAR(32) NOT NULL,
                created_ts BIGINT NOT NULL,
                updated_ts BIGINT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS tenant_credentials (
                tenant_id VARCHAR(64) PRIMARY KEY,
                phone_e164 VARCHAR(32) NOT NULL,
                tg_api_id BIGINT NOT NULL,
                tg_api_hash_encrypted TEXT NOT NULL,
                tg_session_encrypted LONGTEXT NOT NULL,
                encryption_version VARCHAR(32) NOT NULL,
                updated_ts BIGINT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS tenant_runtime (
                tenant_id VARCHAR(64) PRIMARY KEY,
                container_name VARCHAR(255) NOT NULL,
                endpoint_base_url VARCHAR(1024) NOT NULL,
                runtime_status VARCHAR(32) NOT NULL,
                service_port INT NOT NULL,
                last_health_ts BIGINT,
                last_error TEXT,
                updated_ts BIGINT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS bale_bindings (
                bale_user_id VARCHAR(64) NOT NULL,
                bale_chat_id VARCHAR(64) NOT NULL,
                tenant_id VARCHAR(64) NOT NULL,
                is_active TINYINT NOT NULL DEFAULT 1,
                bound_ts BIGINT NOT NULL,
                updated_ts BIGINT NOT NULL,
                PRIMARY KEY (bale_user_id, bale_chat_id),
                INDEX idx_binding_tenant (tenant_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS requests (
                request_id VARCHAR(64) PRIMARY KEY,
                tenant_id VARCHAR(64) NULL,
                bale_user_id VARCHAR(64) NOT NULL,
                bale_chat_id VARCHAR(64) NOT NULL,
                input_type VARCHAR(32) NOT NULL,
                input_text LONGTEXT,
                input_caption LONGTEXT,
                input_file_name VARCHAR(255),
                input_file_size BIGINT,
                input_mime_type VARCHAR(255),
                queue_entered_ts BIGINT NOT NULL,
                mira_sent_ts BIGINT,
                completed_ts BIGINT,
                status VARCHAR(32) NOT NULL,
                error_code VARCHAR(128),
                error_message LONGTEXT,
                output_text LONGTEXT,
                output_media_count INT NOT NULL DEFAULT 0,
                output_payload_json LONGTEXT,
                created_ts BIGINT NOT NULL,
                INDEX idx_requests_created_ts (created_ts),
                INDEX idx_requests_status (status),
                INDEX idx_requests_tenant_id (tenant_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id VARCHAR(64) PRIMARY KEY,
                mode VARCHAR(32) NOT NULL DEFAULT 'chat',
                tts_enabled TINYINT NOT NULL DEFAULT 0,
                updated_ts BIGINT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                tenant_id VARCHAR(64) NOT NULL,
                actor VARCHAR(128) NOT NULL,
                event_type VARCHAR(128) NOT NULL,
                payload_json LONGTEXT,
                created_ts BIGINT NOT NULL,
                INDEX idx_audit_tenant (tenant_id),
                INDEX idx_audit_created (created_ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS registration_requests (
                bale_user_id VARCHAR(64) NOT NULL,
                bale_chat_id VARCHAR(64) NOT NULL,
                status VARCHAR(64) NOT NULL,
                desired_username VARCHAR(255),
                phone_e164 VARCHAR(32),
                note TEXT,
                tenant_id VARCHAR(64),
                created_ts BIGINT NOT NULL,
                updated_ts BIGINT NOT NULL,
                PRIMARY KEY (bale_user_id, bale_chat_id),
                INDEX idx_reg_status (status),
                INDEX idx_reg_updated (updated_ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS tenant_sync_state (
                tenant_id VARCHAR(64) PRIMARY KEY,
                config_digest VARCHAR(128) NOT NULL,
                updated_ts BIGINT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        ]

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Keep startup idempotent without flooding stdout/stderr with MySQL "already exists" warnings.
                await cur.execute("SET SESSION sql_notes = 0")
                for statement in schema_statements:
                    await cur.execute(statement)
                await cur.execute("SET SESSION sql_notes = 1")

    async def _execute(self, query: str, args: tuple[object, ...], *, return_rowcount: bool = False) -> int:
        if self._is_sqlite():
            conn = self._require_sqlite()
            cursor = await conn.execute(query, args)
            await conn.commit()
            return int(cursor.rowcount or 0) if return_rowcount else 0

        pool = self._require_mysql_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                return int(cur.rowcount or 0) if return_rowcount else 0

    async def _fetchone(self, query: str, args: tuple[object, ...]) -> dict[str, object] | None:
        if self._is_sqlite():
            conn = self._require_sqlite()
            async with conn.execute(query, args) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

        pool = self._require_mysql_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, args)
                row = await cur.fetchone()
        if row is None:
            return None
        return dict(row)

    async def _fetchall(self, query: str, args: tuple[object, ...]) -> list[dict[str, object]]:
        if self._is_sqlite():
            conn = self._require_sqlite()
            async with conn.execute(query, args) as cursor:
                rows = await cursor.fetchall()
            return [dict(row) for row in rows]

        pool = self._require_mysql_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, args)
                rows = await cur.fetchall()
        return [dict(row) for row in rows]

    def _require_sqlite(self) -> aiosqlite.Connection:
        if self._sqlite_conn is None:
            raise RuntimeError("SQLite connection is not initialized")
        return self._sqlite_conn

    def _require_mysql_pool(self) -> aiomysql.Pool:
        if self._mysql_pool is None:
            raise RuntimeError("MySQL pool is not initialized")
        return self._mysql_pool


def _now_ts() -> int:
    return int(time.time())
