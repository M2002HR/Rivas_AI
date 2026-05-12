from rivas.text_utils import dedupe_preserve_order, enforce_rivas_branding, split_text


def test_split_text_small():
    text = "سلام دنیا"
    assert split_text(text, 10) == ["سلام دنیا"]


def test_split_text_breaks_paragraphs_and_words():
    text = """این یک پاراگراف تستی نسبتاً طولانی است که باید شکسته شود.

پاراگراف دوم هم باید در خروجی باشد."""
    parts = split_text(text, 40)
    assert len(parts) >= 2
    assert all(len(p) <= 40 for p in parts)


def test_split_text_hard_split_for_long_token():
    token = "الف" * 120
    parts = split_text(token, 30)
    expected_chunks = (len(token) + 29) // 30
    assert len(parts) == expected_chunks
    assert all(len(p) <= 30 for p in parts)


def test_dedupe_preserve_order():
    values = ["a", "b", "a", "c", "b"]
    assert dedupe_preserve_order(values) == ["a", "b", "c"]


def test_enforce_rivas_branding_rewrites_mira_mentions():
    raw = "سلام، من میرا هستم. I am Mira. from mira service."
    fixed = enforce_rivas_branding(raw)
    assert "میرا" not in fixed
    assert "Mira" not in fixed
    assert "mira" not in fixed
    assert "ریواس" in fixed


def test_enforce_rivas_branding_removes_identity_disclaimer():
    raw = "متاسفم، نمی‌تونم اسمم رو عوض کنم.\nمن میرا هستم.\nچطور کمک کنم؟"
    fixed = enforce_rivas_branding(raw)
    assert "اسمم رو عوض" in fixed
    assert "ریواس" in fixed
