from __future__ import annotations

from pathlib import Path

import pytest

from rivas.models import InputType, MiraResponse, RequestPayload
from rivas.storage import Storage


@pytest.mark.asyncio
async def test_storage_request_lifecycle(tmp_path: Path):
    db_path = tmp_path / "rivas_test.db"
    storage = Storage(db_path, retention_days=7)
    await storage.init()

    payload = RequestPayload(
        request_id="req_1",
        bale_user_id="u1",
        bale_chat_id="c1",
        input_type=InputType.TEXT,
        text="hello",
    )

    await storage.create_request(payload)
    await storage.mark_processing(payload.request_id)

    response = MiraResponse(text_blocks=["answer"])
    await storage.mark_completed(payload.request_id, response)

    row = await storage.get_request_snapshot(payload.request_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["output_text"] == "answer"

    await storage.close()


@pytest.mark.asyncio
async def test_storage_user_settings(tmp_path: Path):
    db_path = tmp_path / "rivas_settings.db"
    storage = Storage(db_path, retention_days=7)
    await storage.init()

    before = await storage.get_user_settings("u10")
    assert before["mode"] == "chat"
    assert before["tts_enabled"] is False

    await storage.set_user_mode("u10", "web_search")
    await storage.set_user_tts("u10", True)

    after = await storage.get_user_settings("u10")
    assert after["mode"] == "web_search"
    assert after["tts_enabled"] is True

    await storage.close()
