from __future__ import annotations

from rivas.log_channel import TelegramLogSink


def test_log_channel_formats_message_with_emoji_and_context() -> None:
    sink = TelegramLogSink(
        enabled=True,
        bot_token="token",
        chat_target="@logs",
        min_level="INFO",
    )
    text = sink._format_text(  # noqa: SLF001 - tested intentionally
        "ERROR",
        "Tenant Runtime Unavailable",
        "Runtime is down",
        {"tenant_id": "tenant_1", "bale_user_id": "123"},
    )

    assert "❌ <b>Tenant Runtime Unavailable</b>" in text
    assert "<b>Level:</b> <code>ERROR</code>" in text
    assert "<b>Message:</b> Runtime is down" in text
    assert "<code>tenant_id</code>: <code>tenant_1</code>" in text
    assert "<code>bale_user_id</code>: <code>123</code>" in text


def test_log_channel_truncates_long_messages() -> None:
    sink = TelegramLogSink(
        enabled=True,
        bot_token="token",
        chat_target="@logs",
        min_level="INFO",
    )
    text = sink._format_text(  # noqa: SLF001 - tested intentionally
        "INFO",
        "Long",
        "x" * 5000,
        None,
    )
    assert len(text) <= sink._MAX_TEXT_LEN  # noqa: SLF001 - tested intentionally
    assert text.endswith("…(truncated)")
