from __future__ import annotations

import asyncio
from datetime import datetime
from html import escape
from typing import Final

import aiohttp

from .logging_utils import get_logger, log_event


class TelegramLogSink:
    _API_BASE: Final[str] = "https://api.telegram.org"
    _MAX_TEXT_LEN: Final[int] = 3900
    _LEVEL_EMOJI: Final[dict[str, str]] = {
        "DEBUG": "🔍",
        "INFO": "ℹ️",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "CRITICAL": "🚨",
    }

    def __init__(self, *, enabled: bool, bot_token: str | None, chat_target: str | None, min_level: str) -> None:
        self._enabled = enabled and bool(bot_token) and bool(chat_target)
        self._bot_token = (bot_token or "").strip()
        self._chat_target = (chat_target or "").strip()
        self._min_level = (min_level or "WARNING").upper()
        self._log = get_logger("rivas.log_channel")
        self._levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

    def enabled(self) -> bool:
        return self._enabled

    async def send(self, level: str, title: str, message: str, context: dict[str, object] | None = None) -> None:
        if not self._enabled:
            return
        normalized_level = level.upper()
        if self._levels.get(normalized_level, 0) < self._levels.get(self._min_level, 30):
            return

        url = f"{self._API_BASE}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_target,
            "text": self._format_text(normalized_level, title, message, context),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        timeouts = (8, 12, 16)
        last_error: str | None = None
        for attempt, timeout_seconds in enumerate(timeouts, start=1):
            try:
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                # trust_env allows using HTTP(S)_PROXY/http(s)_proxy when configured.
                async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                    async with session.post(url, json=payload) as response:
                        body = await response.text()
                        if response.status < 400:
                            return
                        last_error = f"status={response.status} body={body[:400]}"
                        # 4xx is usually permanent for this payload; no point retrying.
                        if 400 <= response.status < 500:
                            break
            except Exception as exc:
                last_error = str(exc)
            if attempt < len(timeouts):
                await asyncio.sleep(attempt)

        log_event(self._log, "log_channel_send_failed", error=last_error or "unknown_error")

    def send_background(self, level: str, title: str, message: str, context: dict[str, object] | None = None) -> None:
        if not self._enabled:
            return
        try:
            asyncio.create_task(self.send(level, title, message, context))
        except RuntimeError:
            # No running event loop (unlikely in bot path)
            return

    def _format_text(self, level: str, title: str, message: str, context: dict[str, object] | None) -> str:
        icon = self._LEVEL_EMOJI.get(level, "🧩")
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        lines: list[str] = [
            f"{icon} <b>{escape(title.strip() or 'Rivas Log')}</b>",
            f"🏷️ <b>Level:</b> <code>{escape(level)}</code>",
            f"🕒 <b>Time:</b> <code>{escape(timestamp)}</code>",
            f"💬 <b>Message:</b> {escape((message or '').strip() or '-')}",
        ]

        if context:
            lines.append("📌 <b>Context:</b>")
            for key in sorted(context.keys()):
                value = context[key]
                lines.append(f"• <code>{escape(str(key))}</code>: <code>{escape(str(value))}</code>")

        text = "\n".join(lines)
        if len(text) > self._MAX_TEXT_LEN:
            suffix = "\n…(truncated)"
            limit = max(0, self._MAX_TEXT_LEN - len(suffix))
            text = f"{text[:limit]}{suffix}"
        return text
