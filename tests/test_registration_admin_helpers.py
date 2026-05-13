from __future__ import annotations

from rivas.registration_admin import _suggest_tenant_slug


def test_suggest_tenant_slug_from_username():
    assert _suggest_tenant_slug("Mhr-User 2026", "301657382") == "mhr-user-2026"


def test_suggest_tenant_slug_fallback_to_bale_user_id():
    assert _suggest_tenant_slug("  ", "301657382") == "user-301657382"
