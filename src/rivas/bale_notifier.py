from __future__ import annotations

import json

import aiohttp

BALE_API_BASE_URL = "https://tapi.bale.ai/"


class BaleNotifyError(RuntimeError):
    """Raised when sending a Bale notification message fails."""


async def send_text_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    timeout_seconds: int = 25,
) -> None:
    if not bot_token.strip():
        raise BaleNotifyError("Bale bot token is empty")
    if not chat_id.strip():
        raise BaleNotifyError("Bale chat_id is empty")
    if not text.strip():
        raise BaleNotifyError("Notification text is empty")

    url = f"{BALE_API_BASE_URL}bot{bot_token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": text,
    }
    timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise BaleNotifyError(f"Bale API HTTP {resp.status}: {body[:200]}")

            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise BaleNotifyError("Invalid JSON response from Bale API") from exc

            if not bool(data.get("ok")):
                description = str(data.get("description") or "unknown_error")
                raise BaleNotifyError(f"Bale API error: {description}")
