from rivas.mira_bridge import _build_prompt, _is_placeholder
from rivas.models import InputType, RequestPayload


def _payload(input_type: InputType, text: str | None = None, caption: str | None = None) -> RequestPayload:
    return RequestPayload(
        request_id="r1",
        bale_user_id="u1",
        bale_chat_id="c1",
        input_type=input_type,
        text=text,
        caption=caption,
    )


def test_build_prompt_web_search_contains_instruction():
    prompt = _build_prompt(_payload(InputType.WEB_SEARCH, text="قیمت دلار"))
    assert "جستجو" in prompt
    assert "قیمت دلار" in prompt


def test_build_prompt_photo_uses_caption():
    prompt = _build_prompt(_payload(InputType.PHOTO, caption="این نمودار را بخوان"))
    assert "تحلیل" in prompt
    assert "نمودار" in prompt


def test_placeholder_detection():
    assert _is_placeholder("thinking...")
    assert _is_placeholder("در حال پردازش")
    assert not _is_placeholder("این پاسخ نهایی است")
