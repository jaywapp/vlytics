"""Production worker wiring for the approved KOVO adapter, using offline HTTP."""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text

from vlytics.config import OperationalConfig, Settings
from vlytics.mirror.backfill import BackfillScope, PostgresScopeLease
from vlytics.worker import build_production_worker

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2098, 9, 27, 12, tzinfo=UTC)


class PostgresRoleUrls(Protocol):
    collector: str
    engine: str


def _operational_config(group_code: str) -> OperationalConfig:
    config_path = REPOSITORY_ROOT / "config" / "example.toml"
    schema_path = REPOSITORY_ROOT / "contracts" / "config.schema.json"
    with config_path.open("rb") as stream:
        values = tomllib.load(stream)
    values["source"] = {
        "bulk_collection_enabled": True,
        "permission_policy": "approved-synthetic-test-policy",
        "max_requests_per_minute": 600,
        "max_concurrency": 1,
        "scopes": [
            {
                "source": "kovo",
                "group_code": group_code,
                "season_code": "023",
                "competition_code": "201",
            }
        ],
    }
    return OperationalConfig(values, config_path, schema_path)


def _write_registry(path: Path, group_code: str) -> None:
    path.write_text(
        f'''schema_version = "1.0"

[[franchises]]
source = "kovo"
group_code = "{group_code}"
season_code = "023"
team_code = "TEST01"
franchise_id = "{uuid4()}"
mapping_version = "reviewed-synthetic-v1"
evidence = "offline production wiring fixture"

[[season_rules]]
source = "kovo"
group_code = "{group_code}"
season_code = "023"
mapping_version = "reviewed-synthetic-rules-v1"
evidence = "offline production wiring fixture"
regular_set_target = 25
deciding_set_target = 15
winning_margin = 2
sets_to_win = 3
maximum_sets = 5
''',
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_production_worker_runs_mocked_kovo_adapter_with_separate_db_roles(
    postgres_role_urls: PostgresRoleUrls,
    tmp_path: Path,
) -> None:
    group_code = f"test-{uuid4().hex}"
    registry_path = tmp_path / "source.toml"
    _write_registry(registry_path, group_code)
    requests: list[httpx.Request] = []

    def source_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "user-api.kovo.co.kr"
        assert request.url.params["gcode"] == group_code
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "result": {"status": 200, "message": "synthetic"},
                    "payload": {
                        "content": [],
                        "page": {
                            "number": 0,
                            "size": 2,
                            "totalElements": 0,
                            "totalPages": 1,
                        },
                    },
                }
            ).encode(),
        )

    source_client = httpx.Client(
        transport=httpx.MockTransport(source_response),
        follow_redirects=False,
        trust_env=False,
    )
    settings = Settings(
        database_url=postgres_role_urls.engine,
        collector_database_url=postgres_role_urls.collector,
        source_registry_path=registry_path,
    )
    worker = build_production_worker(
        settings,
        _operational_config(group_code),
        environ={"VLYTICS_DATABASE_URL": postgres_role_urls.engine},
        source_client=source_client,
        now=lambda: NOW,
    )

    engine = create_engine(postgres_role_urls.engine)
    collector = create_engine(postgres_role_urls.collector)
    try:
        assert await worker.run_once() >= 1

        with engine.connect() as connection:
            job_state = connection.execute(
                text(
                    """
                    SELECT state
                    FROM ops.jobs
                    WHERE job_key LIKE :prefix
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"prefix": f"schedule:kovo:{group_code}:%"},
            ).scalar_one()
        with collector.connect() as connection:
            receipts = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM mirror.raw_snapshots
                    WHERE source='kovo' AND source_group_code=:group_code
                    """
                ),
                {"group_code": group_code},
            ).scalar_one()

        assert job_state == "succeeded"
        assert receipts == 1
        assert len(requests) == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE ops.jobs
                    SET state='cancelled', lease_owner=NULL,
                        lease_started_at=NULL, lease_until=NULL, updated_at=:now
                    WHERE payload->>'group_code'=:group_code
                      AND state IN ('queued', 'running', 'retry_wait')
                    """
                ),
                {"group_code": group_code, "now": NOW},
            )
        source_client.close()
        collector.dispose()
        engine.dispose()


def test_global_source_lease_is_shared_across_database_connections(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = create_engine(postgres_role_urls.collector)
    first_scope = BackfillScope("kovo", "001", "023", "201")
    second_scope = BackfillScope("kovo", "001", "024", "201")
    with engine.connect() as first_connection, engine.connect() as second_connection:
        first = PostgresScopeLease(first_connection, lease_key="source:kovo:global")
        second = PostgresScopeLease(second_connection, lease_key="source:kovo:global")
        slot = first.acquire(first_scope, 1)
        assert slot == 0
        try:
            assert second.acquire(second_scope, 1) is None
        finally:
            first.release(first_scope, slot)
        assert second.acquire(second_scope, 1) == 0
        second.release(second_scope, 0)
