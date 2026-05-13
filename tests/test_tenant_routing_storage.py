from __future__ import annotations

from pathlib import Path

import pytest

from rivas.config import DatabaseConfig
from rivas.storage import Storage


@pytest.mark.asyncio
async def test_tenant_binding_resolve(tmp_path: Path):
    db_path = tmp_path / "tenant_route.db"
    storage = Storage(
        DatabaseConfig(
            backend="sqlite",
            db_url=f"sqlite:///{db_path}",
            sqlite_path=db_path,
            mysql_host=None,
            mysql_port=None,
            mysql_user=None,
            mysql_password=None,
            mysql_database=None,
        ),
        retention_days=30,
    )
    await storage.init()

    await storage.upsert_tenant("t1", "tenant-one", "Owner One", "active")
    await storage.upsert_tenant_runtime(
        tenant_id="t1",
        container_name="rivas-mira-tenant-one",
        endpoint_base_url="http://tenant-one-mira:8090",
        runtime_status="running",
    )
    await storage.bind_bale_user("t1", "u1", "c1")

    binding = await storage.resolve_tenant_binding("u1", "c1")
    assert binding is not None
    assert binding.tenant_id == "t1"
    assert binding.endpoint_base_url == "http://tenant-one-mira:8090"
    assert binding.runtime_status == "running"

    await storage.close()
