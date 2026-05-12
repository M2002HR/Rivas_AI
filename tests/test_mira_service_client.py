import base64

from rivas.mira_service_client import _json_to_response, _payload_to_json
from rivas.models import InputType, RequestPayload


def test_payload_to_json_encodes_media():
    payload = RequestPayload(
        request_id="r1",
        bale_user_id="u1",
        bale_chat_id="c1",
        input_type=InputType.PHOTO,
        text="hello",
        media_bytes=b"abc123",
        file_name="x.jpg",
        mime_type="image/jpeg",
    )

    body = _payload_to_json(payload)
    assert body["request_id"] == "r1"
    assert body["input_type"] == "photo"
    assert body["media_base64"] == base64.b64encode(b"abc123").decode("ascii")


def test_json_to_response_decodes_media():
    raw = {
        "text_blocks": ["سلام"],
        "source_message_ids": [11],
        "media_parts": [
            {
                "media_type": "photo",
                "file_name": "img.jpg",
                "mime_type": "image/jpeg",
                "source_message_id": 11,
                "data_base64": base64.b64encode(b"xyz").decode("ascii"),
            }
        ],
    }

    response = _json_to_response(raw)
    assert response.text == "سلام"
    assert response.source_message_ids == [11]
    assert len(response.media_parts) == 1
    assert response.media_parts[0].data == b"xyz"
