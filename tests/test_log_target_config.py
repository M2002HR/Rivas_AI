from __future__ import annotations

from rivas.config import _normalize_chat_username, _select_chat_target


def test_normalize_chat_username():
    assert _normalize_chat_username(None) is None
    assert _normalize_chat_username("") is None
    assert _normalize_chat_username("my_channel") == "@my_channel"
    assert _normalize_chat_username("@my_channel") == "@my_channel"


def test_select_chat_target_prefers_username():
    assert _select_chat_target(username="@ch", chat_id="123") == "@ch"
    assert _select_chat_target(username=None, chat_id="123") == "123"
    assert _select_chat_target(username=None, chat_id=None) is None
