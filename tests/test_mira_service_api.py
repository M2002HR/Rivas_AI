import pytest

from rivas.config import MiraServiceConfig
from rivas.mira_service_api import MiraServiceAPI
from rivas.models import InputType


def _config() -> MiraServiceConfig:
    return MiraServiceConfig(
        tg_api_id=1,
        tg_api_hash="hash",
        tg_string_session="",
        mira_username="@mira",
        service_api_key=None,
        app_env="test",
        log_level="INFO",
        quiet_window_seconds=4,
        first_response_timeout_seconds=45,
        overall_timeout_seconds=150,
        relay_hard_timeout_seconds=210,
        floodwait_hard_limit_seconds=120,
        api_host="127.0.0.1",
        api_port=8090,
        max_payload_mb=20,
    )


def test_json_to_payload_text():
    api = MiraServiceAPI(_config())
    payload = api._json_to_payload(
        {
            "request_id": "abc",
            "input_type": "text",
            "text": "hello",
            "meta": {"bale_user_id": "u1", "bale_chat_id": "c1", "user_mode": "chat"},
        },
        forced_input_type=None,
    )

    assert payload.request_id == "abc"
    assert payload.input_type == InputType.TEXT
    assert payload.text == "hello"
    assert payload.bale_user_id == "u1"


def test_json_to_payload_photo_requires_media():
    api = MiraServiceAPI(_config())
    with pytest.raises(ValueError):
        api._json_to_payload(
            {
                "input_type": "photo",
                "caption": "test",
            },
            forced_input_type=None,
        )
