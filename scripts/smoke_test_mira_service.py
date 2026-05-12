from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBMODULE_SRC = ROOT / "submodules" / "mira-telegram-service" / "src"
if SUBMODULE_SRC.exists():
    sys.path.insert(0, str(SUBMODULE_SRC))

from mira_telegram_service.client import MiraServiceClient
from mira_telegram_service.models import InputType, RequestPayload


async def main() -> int:
    base_url = (os.getenv("MIRA_SERVICE_URL", "http://127.0.0.1:8090") or "http://127.0.0.1:8090").rstrip("/")
    api_key = os.getenv("MIRA_SERVICE_API_KEY")
    text = os.getenv("SMOKE_TEXT", "سلام، لطفاً یک پاسخ کوتاه بده")

    client = MiraServiceClient(base_url=base_url, api_key=api_key, timeout_seconds=320)
    await client.start()

    try:
        ready = await client.is_ready()
        print(f"ready={ready}")
        if not ready:
            print("service is not ready", file=sys.stderr)
            return 2

        payload = RequestPayload(
            request_id="smoke-test",
            bale_user_id="smoke",
            bale_chat_id="smoke",
            input_type=InputType.TEXT,
            text=text,
        )

        result = await client.relay(payload)
        print("response_text:")
        print(result.text or "<empty>")
        print(f"media_parts={len(result.media_parts)}")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
