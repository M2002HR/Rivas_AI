from __future__ import annotations

import pytest

from rivas.bale_notifier import BaleNotifyError, send_text_message


@pytest.mark.asyncio
async def test_send_text_message_validates_required_fields():
    with pytest.raises(BaleNotifyError):
        await send_text_message(bot_token="", chat_id="1", text="x")

    with pytest.raises(BaleNotifyError):
        await send_text_message(bot_token="t", chat_id="", text="x")

    with pytest.raises(BaleNotifyError):
        await send_text_message(bot_token="t", chat_id="1", text=" ")
