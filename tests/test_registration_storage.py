from __future__ import annotations

from pathlib import Path

import pytest

from rivas.config import DatabaseConfig
from rivas.load_users import DesiredUser, _mark_registration_as_provisioned
from rivas.models import RegistrationRequest, RegistrationStatus
from rivas.storage import Storage


def _sqlite_cfg(path: Path) -> DatabaseConfig:
    return DatabaseConfig(
        backend="sqlite",
        db_url=f"sqlite:///{path}",
        sqlite_path=path,
        mysql_host=None,
        mysql_port=None,
        mysql_user=None,
        mysql_password=None,
        mysql_database=None,
    )


@pytest.mark.asyncio
async def test_registration_request_roundtrip(tmp_path: Path):
    db_path = tmp_path / "rivas_reg.db"
    storage = Storage(_sqlite_cfg(db_path), retention_days=7)
    await storage.init()

    req = RegistrationRequest(
        bale_user_id="u1",
        bale_chat_id="c1",
        status=RegistrationStatus.AWAITING_USERNAME,
    )
    await storage.upsert_registration_request(req)

    row = await storage.get_registration_request("u1", "c1")
    assert row is not None
    assert row.status == RegistrationStatus.AWAITING_USERNAME

    req.status = RegistrationStatus.PENDING_ADMIN
    req.desired_username = "ali"
    req.phone_e164 = "+989111111111"
    await storage.upsert_registration_request(req)

    row2 = await storage.get_registration_request("u1", "c1")
    assert row2 is not None
    assert row2.status == RegistrationStatus.PENDING_ADMIN
    assert row2.desired_username == "ali"
    assert row2.phone_e164 == "+989111111111"

    pending = await storage.list_pending_registration_requests()
    assert any(item["bale_user_id"] == "u1" for item in pending)

    await storage.close()


@pytest.mark.asyncio
async def test_tenant_sync_state_roundtrip(tmp_path: Path):
    db_path = tmp_path / "rivas_sync.db"
    storage = Storage(_sqlite_cfg(db_path), retention_days=7)
    await storage.init()

    await storage.upsert_tenant_sync_state("tenant_1", "digest_v1")
    assert await storage.get_tenant_sync_state("tenant_1") == "digest_v1"

    await storage.upsert_tenant_sync_state("tenant_1", "digest_v2")
    assert await storage.get_tenant_sync_state("tenant_1") == "digest_v2"

    await storage.delete_tenant_sync_state("tenant_1")
    assert await storage.get_tenant_sync_state("tenant_1") is None

    await storage.close()


@pytest.mark.asyncio
async def test_mark_registration_as_provisioned_returns_true_only_once(tmp_path: Path):
    db_path = tmp_path / "rivas_mark_reg.db"
    storage = Storage(_sqlite_cfg(db_path), retention_days=7)
    await storage.init()

    req = RegistrationRequest(
        bale_user_id="u2",
        bale_chat_id="c2",
        status=RegistrationStatus.PENDING_ADMIN,
        desired_username="user2",
        phone_e164="+989122222222",
    )
    await storage.upsert_registration_request(req)

    user = DesiredUser(
        tenant_id="tenant_u2",
        tenant_slug="u2",
        owner_name="u2",
        status="active",
        bale_user_id="u2",
        bale_chat_id="c2",
        phone_e164="+989122222222",
        tg_api_id=111,
        tg_api_hash="hash",
        tg_string_session="sess",
    )

    changed_first = await _mark_registration_as_provisioned(storage, user)
    changed_second = await _mark_registration_as_provisioned(storage, user)

    assert changed_first is True
    assert changed_second is False

    row = await storage.get_registration_request("u2", "c2")
    assert row is not None
    assert row.status == RegistrationStatus.PROVISIONED
    assert row.tenant_id == "tenant_u2"
    assert row.note == "Provisioned by load-users"

    await storage.close()
