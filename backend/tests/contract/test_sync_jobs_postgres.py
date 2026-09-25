"""PostgreSQL integration for idempotent sync job execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import create_engine, text

from vlytics.ops.repositories.jobs import JobRepository
from vlytics.ops.sync import SyncJobDispatcher


class PostgresRoleUrls(Protocol):
    engine: str


class RecordingHandler:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def handle(self, job: dict[str, Any], *, now: datetime) -> None:
        del now
        self.keys.append(str(job["job_key"]))


def test_job_enqueue_is_idempotent_and_dispatcher_finishes_once(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    now = datetime.now(UTC)
    job_key = f"sync-idempotent-{uuid4().hex}"
    engine = create_engine(postgres_role_urls.engine)

    with engine.begin() as connection:
        repository = JobRepository(connection)
        first = repository.enqueue(
            job_key=job_key,
            job_type="mirror.current_schedule",
            payload={"source": "synthetic"},
            due_at=now,
            deadline_at=now + timedelta(minutes=5),
        )
        repeated = repository.enqueue(
            job_key=job_key,
            job_type="mirror.current_schedule",
            payload={"source": "changed-value-must-not-overwrite"},
            due_at=now + timedelta(minutes=1),
            deadline_at=now + timedelta(minutes=6),
        )

        assert repeated["id"] == first["id"]
        assert repeated["payload"] == {"source": "synthetic"}
        assert repeated["due_at"] == now

        handler = RecordingHandler()
        dispatcher = SyncJobDispatcher(
            repository,
            {"mirror.current_schedule": handler},
            lease_owner="postgres-test-worker",
        )
        assert dispatcher.run_once(now=now) == 1
        assert dispatcher.run_once(now=now) == 0

        state = connection.execute(
            text("SELECT state, attempt_no FROM ops.jobs WHERE job_key=:job_key"),
            {"job_key": job_key},
        ).one()

    assert handler.keys == [job_key]
    assert state == ("succeeded", 1)
