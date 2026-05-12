from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from .models import MiraResponse, RequestPayload, RequestStatus


class Storage:
    def __init__(self, db_path: Path, retention_days: int) -> None:
        self._db_path = db_path
        self._retention_days = retention_days
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
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

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'chat',
                tts_enabled INTEGER NOT NULL DEFAULT 0,
                updated_ts INTEGER NOT NULL
            );
            """
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    async def create_request(self, payload: RequestPayload) -> None:
        conn = self._require_conn()
        now = _now_ts()
        await conn.execute(
            """
            INSERT INTO requests (
                request_id, bale_user_id, bale_chat_id, input_type,
                input_text, input_caption, input_file_name, input_file_size, input_mime_type,
                queue_entered_ts, status, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.request_id,
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
            ),
        )
        await conn.commit()

    async def mark_processing(self, request_id: str) -> None:
        conn = self._require_conn()
        await conn.execute(
            "UPDATE requests SET status = ?, mira_sent_ts = ? WHERE request_id = ?",
            (RequestStatus.PROCESSING.value, _now_ts(), request_id),
        )
        await conn.commit()

    async def mark_failed(self, request_id: str, error_code: str, error_message: str) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            UPDATE requests
            SET status = ?, error_code = ?, error_message = ?, completed_ts = ?
            WHERE request_id = ?
            """,
            (
                RequestStatus.FAILED.value,
                error_code,
                error_message,
                _now_ts(),
                request_id,
            ),
        )
        await conn.commit()

    async def mark_completed(self, request_id: str, response: MiraResponse) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            UPDATE requests
            SET status = ?, completed_ts = ?, output_text = ?, output_media_count = ?, output_payload_json = ?
            WHERE request_id = ?
            """,
            (
                RequestStatus.COMPLETED.value,
                _now_ts(),
                response.text,
                len(response.media_parts),
                json.dumps(response.as_json(), ensure_ascii=False),
                request_id,
            ),
        )
        await conn.commit()

    async def set_user_mode(self, user_id: str, mode: str) -> None:
        conn = self._require_conn()
        now = _now_ts()
        await conn.execute(
            """
            INSERT INTO user_settings (user_id, mode, tts_enabled, updated_ts)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET mode = excluded.mode, updated_ts = excluded.updated_ts
            """,
            (user_id, mode, now),
        )
        await conn.commit()

    async def set_user_tts(self, user_id: str, enabled: bool) -> None:
        conn = self._require_conn()
        now = _now_ts()
        await conn.execute(
            """
            INSERT INTO user_settings (user_id, mode, tts_enabled, updated_ts)
            VALUES (?, 'chat', ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET tts_enabled = excluded.tts_enabled, updated_ts = excluded.updated_ts
            """,
            (user_id, int(enabled), now),
        )
        await conn.commit()

    async def get_user_settings(self, user_id: str) -> dict[str, object]:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT mode, tts_enabled FROM user_settings WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return {"mode": "chat", "tts_enabled": False}

        return {
            "mode": row["mode"] or "chat",
            "tts_enabled": bool(row["tts_enabled"]),
        }

    async def cleanup_expired(self) -> int:
        conn = self._require_conn()
        threshold = _now_ts() - self._retention_days * 24 * 60 * 60
        cursor = await conn.execute("DELETE FROM requests WHERE created_ts < ?", (threshold,))
        deleted = cursor.rowcount or 0
        await conn.commit()
        return deleted

    async def pending_count(self) -> int:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT COUNT(1) AS c FROM requests WHERE status IN (?, ?)",
            (RequestStatus.QUEUED.value, RequestStatus.PROCESSING.value),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["c"] if row else 0)

    async def get_request_snapshot(self, request_id: str) -> dict[str, object] | None:
        conn = self._require_conn()
        async with conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Storage is not initialized")
        return self._conn


def _now_ts() -> int:
    return int(time.time())
