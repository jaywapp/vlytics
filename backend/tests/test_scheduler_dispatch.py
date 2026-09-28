from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from threading import Event, Lock
from typing import Any
from uuid import UUID, uuid4

import pytest

import vlytics.ops.scheduler as scheduler_module
from vlytics.ops.scheduler import PredictionJobDispatcher, RetryableJobError

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


class _Engine:
    def begin(self) -> nullcontext[object]:
        return nullcontext(object())


class _Repository:
    def __init__(self, jobs: list[Mapping[str, Any]]) -> None:
        self._jobs = list(jobs)
        self._lock = Lock()
        self.retries: list[tuple[UUID, str]] = []
        self.finished: list[tuple[UUID, bool]] = []
        self.renewals: list[UUID] = []
        self.renewed = Event()

    def expire_due(self, *, now: datetime) -> int:
        del now
        return 0

    def lease_next(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> Mapping[str, Any] | None:
        del lease_owner, now, lease_duration
        with self._lock:
            return None if not self._jobs else self._jobs.pop(0)

    def renew_lease(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> bool:
        del lease_owner, now, lease_duration
        with self._lock:
            self.renewals.append(job_id)
        self.renewed.set()
        return True

    def schedule_retry(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        retry_at: datetime,
        error_code: str,
        now: datetime,
    ) -> bool:
        del lease_owner, retry_at, now
        with self._lock:
            self.retries.append((job_id, error_code))
        return True

    def finish(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        succeeded: bool,
        error_code: str | None,
        completed_at: datetime,
    ) -> bool:
        del lease_owner, error_code, completed_at
        with self._lock:
            self.finished.append((job_id, succeeded))
        return True


class _RetryingSourceHandler:
    def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
        del job, now
        raise RetryableJobError("source_request_failed")


class _SuccessfulHandler:
    def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
        del job, now


def _job(job_type: str) -> Mapping[str, Any]:
    return {"id": uuid4(), "job_type": job_type, "attempt_no": 1, "payload": {}}


def test_concurrent_source_retries_do_not_abort_other_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [_job("source"), _job("source"), _job("prediction")]
    repository = _Repository(jobs)
    monkeypatch.setattr(scheduler_module, "JobRepository", lambda _connection: repository)
    dispatcher = PredictionJobDispatcher(
        _Engine(),  # type: ignore[arg-type]
        {"source": _RetryingSourceHandler(), "prediction": _SuccessfulHandler()},
        lease_owner="worker-1",
        max_concurrency=3,
        now=lambda: NOW,
    )

    assert dispatcher.run_once(now=NOW) == 3
    assert sorted(error_code for _, error_code in repository.retries) == [
        "source_request_failed",
        "source_request_failed",
    ]
    assert repository.finished == [(jobs[2]["id"], True)]


def test_long_job_renews_lease_before_handler_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job("long-source")
    repository = _Repository([job])
    monkeypatch.setattr(scheduler_module, "JobRepository", lambda _connection: repository)

    class _LongHandler:
        def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
            del job, now
            assert repository.renewed.wait(timeout=1)

    dispatcher = PredictionJobDispatcher(
        _Engine(),  # type: ignore[arg-type]
        {"long-source": _LongHandler()},
        lease_owner="worker-1",
        lease_duration=timedelta(milliseconds=20),
        max_concurrency=1,
        now=lambda: NOW,
    )

    assert dispatcher.run_once(now=NOW) == 1
    assert repository.renewals == [job["id"]]
    assert repository.finished == [(job["id"], True)]


def test_deadline_stops_renewal_without_reporting_lease_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = {**_job("deadline-source"), "deadline_at": NOW}
    repository = _Repository([job])
    monkeypatch.setattr(scheduler_module, "JobRepository", lambda _connection: repository)

    class _DeadlineHandler:
        def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
            del job, now
            Event().wait(0.03)

    dispatcher = PredictionJobDispatcher(
        _Engine(),  # type: ignore[arg-type]
        {"deadline-source": _DeadlineHandler()},
        lease_owner="worker-1",
        lease_duration=timedelta(milliseconds=20),
        max_concurrency=1,
        now=lambda: NOW,
    )

    assert dispatcher.run_once(now=NOW) == 1
    assert repository.renewals == []
    assert repository.finished == [(job["id"], True)]
