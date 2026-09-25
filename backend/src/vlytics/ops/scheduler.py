"""Durable strict T-60 scheduling, lifecycle reconciliation, and worker dispatch."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from vlytics.config import OperationalConfig, OperationalConfigError
from vlytics.engine.repositories.predictions import PredictionEventRepository
from vlytics.ops.repositories.jobs import JobRepository


class SchedulerJobType(StrEnum):
    SOURCE_SYNC = "mirror.current_schedule"
    FREEZE_SNAPSHOT = "engine.freeze_snapshot"
    RUN_PREDICTION = "engine.run_prediction"


class PredictionKind(StrEnum):
    STATISTICAL = "statistical"
    PROVIDER = "provider"


@dataclass(frozen=True)
class SchedulerTiming:
    """The OP-004 timing contract; defaults are also used by synthetic replay."""

    target_cutoff: timedelta = timedelta(minutes=60)
    initial_start_tolerance: timedelta = timedelta(seconds=30)
    completion_grace: timedelta = timedelta(seconds=300)
    request_timeout: timedelta = timedelta(seconds=60)
    max_attempts: int = 3
    retry_initial_delay: timedelta = timedelta(seconds=5)
    retry_backoff_multiplier: float = 2.0
    retry_max_delay: timedelta = timedelta(seconds=30)
    source_sync_interval: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.target_cutoff != timedelta(minutes=60):
            raise ValueError("strict scheduling requires a T-60 cutoff")
        if not timedelta(0) <= self.initial_start_tolerance < self.completion_grace:
            raise ValueError("initial start tolerance must be shorter than completion grace")
        if self.completion_grace >= self.target_cutoff:
            raise ValueError("completion grace must end before match start")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        worst_case = self.request_timeout * self.max_attempts + sum(
            (self.retry_delay(attempt) for attempt in range(1, self.max_attempts)),
            timedelta(),
        )
        if worst_case > self.completion_grace:
            raise ValueError("request and retry budget exceeds completion grace")

    @classmethod
    def from_operational_config(cls, config: OperationalConfig) -> SchedulerTiming:
        section = config.values.get("timing")
        if not isinstance(section, Mapping):
            raise OperationalConfigError("operational timing section is missing")
        if section.get("allow_t10_retry") is not False:
            raise OperationalConfigError("T-10 retry must remain disabled")
        return cls(
            target_cutoff=timedelta(minutes=_integer(section, "target_cutoff_minutes")),
            initial_start_tolerance=timedelta(
                seconds=_integer(section, "initial_start_tolerance_seconds")
            ),
            completion_grace=timedelta(seconds=_integer(section, "completion_grace_seconds")),
            request_timeout=timedelta(seconds=_integer(section, "request_timeout_seconds")),
            max_attempts=_integer(section, "max_attempts"),
            retry_initial_delay=timedelta(seconds=_integer(section, "retry_initial_delay_seconds")),
            retry_backoff_multiplier=_number(section, "retry_backoff_multiplier"),
            retry_max_delay=timedelta(seconds=_integer(section, "retry_max_delay_seconds")),
        )

    def cutoff_at(self, scheduled_start_at: datetime) -> datetime:
        _require_aware(scheduled_start_at, "scheduled_start_at")
        return scheduled_start_at - self.target_cutoff

    def deadline_at(
        self,
        scheduled_start_at: datetime,
        actual_start_at: datetime | None = None,
    ) -> datetime:
        cutoff = self.cutoff_at(scheduled_start_at)
        candidates = [cutoff + self.completion_grace, scheduled_start_at]
        if actual_start_at is not None:
            _require_aware(actual_start_at, "actual_start_at")
            candidates.append(actual_start_at)
        return min(candidates)

    def retry_delay(self, completed_attempt: int) -> timedelta:
        if completed_attempt < 1:
            raise ValueError("completed_attempt must be positive")
        seconds = self.retry_initial_delay.total_seconds() * (
            self.retry_backoff_multiplier ** (completed_attempt - 1)
        )
        return min(timedelta(seconds=seconds), self.retry_max_delay)

    def start_is_on_time(self, started_at: datetime, scheduled_start_at: datetime) -> bool:
        cutoff = self.cutoff_at(scheduled_start_at)
        return cutoff <= started_at <= cutoff + self.initial_start_tolerance

    def completion_is_on_time(
        self,
        completed_at: datetime,
        scheduled_start_at: datetime,
        actual_start_at: datetime | None = None,
    ) -> bool:
        deadline = self.deadline_at(scheduled_start_at, actual_start_at)
        return (
            completed_at < deadline
            and completed_at < scheduled_start_at
            and (actual_start_at is None or completed_at < actual_start_at)
        )


@dataclass(frozen=True)
class LiveDryRunEvidence:
    """Explicit evidence required before a live scheduler can be constructed."""

    config_sha256: str
    completed_at: datetime
    source_sync_verified: bool
    freeze_verified: bool
    providers_verified: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_aware(self.completed_at, "completed_at")
        if re.fullmatch(r"[0-9a-f]{64}", self.config_sha256) is None:
            raise ValueError("dry-run config hash must be SHA-256")
        if len(set(self.providers_verified)) != len(self.providers_verified):
            raise ValueError("dry-run providers must be unique")


def load_live_dry_run_evidence(
    config: OperationalConfig,
    path: Path | None,
    *,
    now: datetime | None = None,
) -> LiveDryRunEvidence:
    """Load bounded, fresh evidence tied to the exact operational configuration."""

    if path is None:
        raise OperationalConfigError("OP-004 live dry-run evidence path is required")
    activation = config.values.get("activation")
    if not isinstance(activation, Mapping):
        raise OperationalConfigError("operational activation configuration is missing")
    max_bytes = activation.get("dry_run_evidence_max_bytes")
    max_age_hours = activation.get("dry_run_evidence_max_age_hours")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or isinstance(max_age_hours, bool)
        or not isinstance(max_age_hours, int)
    ):
        raise OperationalConfigError("operational activation limits are invalid")
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
            raise OperationalConfigError("OP-004 live dry-run evidence file is invalid")
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise OperationalConfigError("OP-004 live dry-run evidence file is invalid")
        document = json.loads(raw.decode("utf-8-sig"))
    except OperationalConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OperationalConfigError("OP-004 live dry-run evidence file is invalid") from error
    required_keys = {
        "schema_version",
        "config_sha256",
        "completed_at",
        "source_sync_verified",
        "freeze_verified",
        "providers_verified",
    }
    if not isinstance(document, dict) or set(document) != required_keys:
        raise OperationalConfigError("OP-004 live dry-run evidence document is invalid")
    if document.get("schema_version") != "1.0":
        raise OperationalConfigError("OP-004 live dry-run evidence document is invalid")
    completed_at_raw = document.get("completed_at")
    providers_raw = document.get("providers_verified")
    if (
        not isinstance(document.get("config_sha256"), str)
        or not isinstance(completed_at_raw, str)
        or not isinstance(document.get("source_sync_verified"), bool)
        or not isinstance(document.get("freeze_verified"), bool)
        or not isinstance(providers_raw, list)
        or any(not isinstance(provider, str) for provider in providers_raw)
    ):
        raise OperationalConfigError("OP-004 live dry-run evidence document is invalid")
    try:
        completed_at = datetime.fromisoformat(completed_at_raw.replace("Z", "+00:00"))
        evidence = LiveDryRunEvidence(
            config_sha256=document["config_sha256"],
            completed_at=completed_at,
            source_sync_verified=document["source_sync_verified"],
            freeze_verified=document["freeze_verified"],
            providers_verified=tuple(providers_raw),
        )
    except (TypeError, ValueError) as error:
        raise OperationalConfigError("OP-004 live dry-run evidence document is invalid") from error
    checked_at = now or datetime.now(UTC)
    _require_aware(checked_at, "now")
    if evidence.completed_at > checked_at + timedelta(minutes=5):
        raise OperationalConfigError("OP-004 live dry-run evidence timestamp is in the future")
    if checked_at - evidence.completed_at > timedelta(hours=max_age_hours):
        raise OperationalConfigError("OP-004 live dry-run evidence has expired")
    validate_live_scheduler_activation(config, evidence)
    return evidence


def validate_live_scheduler_activation(
    config: OperationalConfig,
    evidence: LiveDryRunEvidence | None,
) -> SchedulerTiming:
    """Fail closed until the exact live config has completed the real dry run."""

    if not config.live_operations_enabled:
        raise OperationalConfigError("live scheduler is disabled")
    if evidence is None:
        raise OperationalConfigError("OP-004 live dry-run evidence is required")
    expected_hash = operational_config_sha256(config)
    providers = set(evidence.providers_verified)
    if (
        evidence.config_sha256 != expected_hash
        or not evidence.source_sync_verified
        or not evidence.freeze_verified
        or providers != {"openai", "anthropic", "google"}
    ):
        raise OperationalConfigError(
            "OP-004 live dry-run evidence does not match the active configuration"
        )
    return SchedulerTiming.from_operational_config(config)


def operational_config_sha256(config: OperationalConfig) -> str:
    payload = json.dumps(
        config.values,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ScheduleRevision:
    match_id: UUID
    id: UUID
    revision: int
    scheduled_start_at: datetime
    actual_start_at: datetime | None
    status: str
    observed_at: datetime
    source_scope: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _require_aware(self.scheduled_start_at, "scheduled_start_at")
        _require_aware(self.observed_at, "observed_at")
        if self.actual_start_at is not None:
            _require_aware(self.actual_start_at, "actual_start_at")
        if self.revision < 1 or not self.status.strip():
            raise ValueError("schedule revision and status must be valid")


@dataclass(frozen=True)
class PredictionVariantSpec:
    id: UUID
    key: str
    kind: PredictionKind

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("prediction variant key must not be blank")


@dataclass(frozen=True)
class ScheduledJob:
    job_key: str
    job_type: SchedulerJobType
    payload: Mapping[str, object]
    due_at: datetime
    deadline_at: datetime
    match_id: UUID
    schedule_revision_id: UUID
    stage: str
    variant_id: UUID | None


class SchedulePlanner:
    """Create replay-safe source, freeze, and independent prediction jobs."""

    def __init__(self, timing: SchedulerTiming | None = None) -> None:
        self.timing = timing or SchedulerTiming()

    def plan(
        self,
        schedules: Iterable[ScheduleRevision],
        variants: Iterable[PredictionVariantSpec],
        *,
        now: datetime,
    ) -> tuple[ScheduledJob, ...]:
        _require_aware(now, "now")
        variants_tuple = tuple(variants)
        keys = [variant.key for variant in variants_tuple]
        if len(keys) != len(set(keys)):
            raise ValueError("prediction variant keys must be unique")
        jobs: list[ScheduledJob] = []
        for schedule in schedules:
            if schedule.status.lower() in {"cancelled", "postponed"}:
                continue
            cutoff = self.timing.cutoff_at(schedule.scheduled_start_at)
            deadline = self.timing.deadline_at(
                schedule.scheduled_start_at, schedule.actual_start_at
            )
            base_payload: dict[str, object] = {
                "match_id": str(schedule.match_id),
                "schedule_revision_id": str(schedule.id),
                "scheduled_start_at": schedule.scheduled_start_at.isoformat(),
                "actual_start_at": (
                    schedule.actual_start_at.isoformat()
                    if schedule.actual_start_at is not None
                    else None
                ),
                "cutoff_at": cutoff.isoformat(),
            }
            if now < cutoff and schedule.source_scope is not None:
                bucket = _bucket(now, self.timing.source_sync_interval)
                jobs.append(
                    ScheduledJob(
                        job_key=(
                            f"source-sync:{schedule.match_id}:{schedule.id}:{bucket.isoformat()}"
                        ),
                        job_type=SchedulerJobType.SOURCE_SYNC,
                        payload={**dict(schedule.source_scope), **base_payload},
                        due_at=now,
                        deadline_at=cutoff,
                        match_id=schedule.match_id,
                        schedule_revision_id=schedule.id,
                        stage="source_sync",
                        variant_id=None,
                    )
                )
            jobs.append(
                ScheduledJob(
                    job_key=f"prediction:{schedule.match_id}:{schedule.id}:freeze",
                    job_type=SchedulerJobType.FREEZE_SNAPSHOT,
                    payload=base_payload,
                    due_at=cutoff,
                    deadline_at=deadline,
                    match_id=schedule.match_id,
                    schedule_revision_id=schedule.id,
                    stage="freeze",
                    variant_id=None,
                )
            )
            for variant in variants_tuple:
                jobs.append(
                    ScheduledJob(
                        job_key=(
                            f"prediction:{schedule.match_id}:{schedule.id}:pregame:{variant.key}"
                        ),
                        job_type=SchedulerJobType.RUN_PREDICTION,
                        payload={
                            **base_payload,
                            "variant_key": variant.key,
                            "prediction_kind": variant.kind.value,
                            "stage": "pregame",
                        },
                        due_at=cutoff,
                        deadline_at=deadline,
                        match_id=schedule.match_id,
                        schedule_revision_id=schedule.id,
                        stage="pregame",
                        variant_id=variant.id,
                    )
                )
        return tuple(jobs)


class DurableScheduler:
    def __init__(self, jobs: JobRepository, planner: SchedulePlanner) -> None:
        self._jobs = jobs
        self._planner = planner

    def enqueue(
        self,
        schedules: Iterable[ScheduleRevision],
        variants: Iterable[PredictionVariantSpec],
        *,
        now: datetime,
    ) -> int:
        planned = self._planner.plan(schedules, variants, now=now)
        for job in planned:
            self._jobs.enqueue(
                job_key=job.job_key,
                job_type=job.job_type.value,
                payload=job.payload,
                due_at=job.due_at,
                deadline_at=job.deadline_at,
                match_id=job.match_id,
                schedule_revision_id=job.schedule_revision_id,
                stage=job.stage,
                variant_id=job.variant_id,
            )
        return len(planned)


class PostgresScheduleReader:
    """Read the newest schedule revision for each match in the worker horizon."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def upcoming(
        self,
        *,
        now: datetime,
        horizon: timedelta = timedelta(days=2),
    ) -> tuple[ScheduleRevision, ...]:
        _require_aware(now, "now")
        rows = self._connection.execute(
            text(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (revision.match_id)
                           revision.*, match.source, match.source_group_code,
                           match.source_season_code, match.source_competition_code
                    FROM mirror.match_revisions AS revision
                    JOIN mirror.matches AS match ON match.id = revision.match_id
                    ORDER BY revision.match_id, revision.revision DESC
                )
                SELECT *
                FROM latest
                WHERE scheduled_start_at >= :oldest
                  AND scheduled_start_at <= :latest
                ORDER BY scheduled_start_at, match_id
                """
            ),
            {
                "oldest": now - timedelta(minutes=60),
                "latest": now + horizon,
            },
        ).mappings()
        return tuple(self._from_row(cast(Mapping[str, Any], row)) for row in rows)

    def previous(self, current: ScheduleRevision) -> ScheduleRevision | None:
        row = (
            self._connection.execute(
                text(
                    """
                    SELECT revision.*, match.source, match.source_group_code,
                           match.source_season_code, match.source_competition_code
                    FROM mirror.match_revisions AS revision
                    JOIN mirror.matches AS match ON match.id = revision.match_id
                    WHERE revision.match_id = :match_id
                      AND revision.revision < :revision
                    ORDER BY revision.revision DESC
                    LIMIT 1
                    """
                ),
                {"match_id": current.match_id, "revision": current.revision},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._from_row(cast(Mapping[str, Any], row))

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> ScheduleRevision:
        return ScheduleRevision(
            match_id=cast(UUID, row["match_id"]),
            id=cast(UUID, row["id"]),
            revision=int(row["revision"]),
            scheduled_start_at=cast(datetime, row["scheduled_start_at"]),
            actual_start_at=cast(datetime | None, row["actual_start_at"]),
            status=str(row["status"]),
            observed_at=cast(datetime, row["observed_at"]),
            source_scope={
                "source": str(row["source"]),
                "group_code": str(row["source_group_code"]),
                "season_code": str(row["source_season_code"]),
                "competition_code": str(row["source_competition_code"]),
            },
        )


class ScheduleService:
    """One scheduler tick: reconcile revisions, then idempotently enqueue work."""

    def __init__(
        self,
        reader: PostgresScheduleReader,
        lifecycle: ScheduleLifecycleCoordinator,
        scheduler: DurableScheduler,
        variants: Iterable[PredictionVariantSpec],
    ) -> None:
        self._reader = reader
        self._lifecycle = lifecycle
        self._scheduler = scheduler
        self._variants = tuple(variants)

    def run_once(self, *, now: datetime) -> int:
        schedules = self._reader.upcoming(now=now)
        for schedule in schedules:
            self._lifecycle.reconcile(schedule, self._reader.previous(schedule))
        return self._scheduler.enqueue(schedules, self._variants, now=now)


class ScheduleEventRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add_once(
        self,
        *,
        match_id: UUID,
        previous_schedule_revision_id: UUID | None,
        schedule_revision_id: UUID,
        event_type: str,
        reason: str,
        occurred_at: datetime,
        observed_at: datetime,
        metadata: Mapping[str, object] | None = None,
    ) -> UUID:
        event_key = (
            f"{match_id}:{previous_schedule_revision_id}:{schedule_revision_id}:{event_type}"
        )
        event_id = self._connection.execute(
            text(
                """
                INSERT INTO ops.schedule_events (
                    event_key, match_id, previous_schedule_revision_id,
                    schedule_revision_id, event_type, reason, occurred_at,
                    observed_at, metadata
                ) VALUES (
                    :event_key, :match_id, :previous_schedule_revision_id,
                    :schedule_revision_id, :event_type, :reason, :occurred_at,
                    :observed_at, CAST(:metadata AS jsonb)
                )
                ON CONFLICT (event_key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "event_key": event_key,
                "match_id": match_id,
                "previous_schedule_revision_id": previous_schedule_revision_id,
                "schedule_revision_id": schedule_revision_id,
                "event_type": event_type,
                "reason": reason,
                "occurred_at": occurred_at,
                "observed_at": observed_at,
                "metadata": json.dumps(dict(metadata or {}), sort_keys=True, separators=(",", ":")),
            },
        ).scalar_one_or_none()
        if event_id is None:
            event_id = self._connection.execute(
                text("SELECT id FROM ops.schedule_events WHERE event_key = :event_key"),
                {"event_key": event_key},
            ).scalar_one()
        return cast(UUID, event_id)


class ScheduleLifecycleCoordinator:
    """Record schedule changes, stop obsolete work, and revoke affected predictions."""

    def __init__(
        self,
        connection: Connection,
        *,
        jobs: JobRepository | None = None,
    ) -> None:
        self._events = ScheduleEventRepository(connection)
        self._jobs = jobs or JobRepository(connection)
        self._predictions = PredictionEventRepository(connection)

    def reconcile(
        self,
        current: ScheduleRevision,
        previous: ScheduleRevision | None,
    ) -> str | None:
        event_type = classify_schedule_change(current, previous)
        if event_type is not None:
            self._events.add_once(
                match_id=current.match_id,
                previous_schedule_revision_id=previous.id if previous else None,
                schedule_revision_id=current.id,
                event_type=event_type,
                reason=f"schedule revision {current.revision}: {event_type}",
                occurred_at=current.observed_at,
                observed_at=current.observed_at,
                metadata={
                    "scheduled_start_at": current.scheduled_start_at.isoformat(),
                    "status": current.status,
                },
            )
        if previous is not None and previous.id != current.id and event_type is not None:
            self._jobs.cancel_match_jobs(
                match_id=current.match_id,
                now=current.observed_at,
                error_code=f"schedule_{event_type}",
                except_schedule_revision_id=current.id,
            )
            self._predictions.void_match_predictions(
                match_id=current.match_id,
                reason=f"schedule_{event_type}",
                occurred_at=current.observed_at,
                observed_at=current.observed_at,
                except_schedule_revision_id=current.id,
            )
        if current.status.lower() in {"cancelled", "postponed"}:
            self._jobs.cancel_match_jobs(
                match_id=current.match_id,
                now=current.observed_at,
                error_code=f"schedule_{current.status.lower()}",
            )
            self._predictions.void_match_predictions(
                match_id=current.match_id,
                reason=f"schedule_{current.status.lower()}",
                occurred_at=current.observed_at,
                observed_at=current.observed_at,
            )
        if current.actual_start_at is not None:
            self._jobs.cancel_match_jobs(
                match_id=current.match_id,
                now=current.observed_at,
                error_code="match_actual_start",
            )
            self._predictions.reject_started_match(
                match_id=current.match_id,
                actual_start_at=current.actual_start_at,
                observed_at=current.observed_at,
            )
        return event_type


class JobHandler(Protocol):
    def handle(self, job: Mapping[str, Any], *, now: datetime) -> bool | None: ...


class RetryableJobError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class TerminalJobError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class SchedulerDispatcher:
    """Run one leased job with OP-004 backoff and restart-safe lease recovery."""

    def __init__(
        self,
        queue: JobRepository,
        handlers: Mapping[str, JobHandler],
        *,
        lease_owner: str,
        timing: SchedulerTiming | None = None,
        lease_duration: timedelta = timedelta(minutes=2),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._queue = queue
        self._handlers = dict(handlers)
        self._lease_owner = lease_owner
        self._timing = timing or SchedulerTiming()
        self._lease_duration = lease_duration
        self._now = now

    def run_once(self, *, now: datetime) -> int:
        self._queue.expire_due(now=now)
        job = self._queue.lease_next(
            lease_owner=self._lease_owner,
            now=now,
            lease_duration=self._lease_duration,
        )
        if job is None:
            return 0
        job_id = job.get("id")
        if not isinstance(job_id, UUID):
            raise TypeError("leased job id must be UUID")
        handler = self._handlers.get(str(job.get("job_type")))
        if handler is None:
            self._queue.quarantine(
                job_id=job_id,
                lease_owner=self._lease_owner,
                error_code="unknown_job_type",
                now=self._now(),
            )
            return 1
        try:
            finalized = handler.handle(job, now=now)
        except RetryableJobError as error:
            completed_at = self._now()
            attempt = int(job["attempt_no"])
            if attempt < self._timing.max_attempts:
                retry_at = completed_at + self._timing.retry_delay(attempt)
                self._queue.schedule_retry(
                    job_id=job_id,
                    lease_owner=self._lease_owner,
                    retry_at=retry_at,
                    error_code=error.error_code,
                    now=completed_at,
                )
            else:
                self._queue.finish(
                    job_id=job_id,
                    lease_owner=self._lease_owner,
                    succeeded=False,
                    error_code=error.error_code,
                    completed_at=completed_at,
                )
            return 1
        except TerminalJobError as error:
            self._queue.quarantine(
                job_id=job_id,
                lease_owner=self._lease_owner,
                error_code=error.error_code,
                now=self._now(),
            )
            return 1
        if finalized is True:
            return 1
        if not self._queue.finish(
            job_id=job_id,
            lease_owner=self._lease_owner,
            succeeded=True,
            error_code=None,
            completed_at=self._now(),
        ):
            raise RuntimeError("job lease was lost before completion")
        return 1


class PredictionJobDispatcher:
    """Lease and execute independent due jobs concurrently on short transactions."""

    def __init__(
        self,
        engine: Engine,
        handlers: Mapping[str, JobHandler],
        *,
        lease_owner: str,
        timing: SchedulerTiming | None = None,
        lease_duration: timedelta = timedelta(minutes=2),
        max_concurrency: int = 4,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self._engine = engine
        self._handlers = dict(handlers)
        self._lease_owner = lease_owner
        self._timing = timing or SchedulerTiming()
        self._lease_duration = lease_duration
        self._max_concurrency = max_concurrency
        self._now = now

    def run_once(self, *, now: datetime) -> int:
        jobs: list[Mapping[str, Any]] = []
        with self._engine.begin() as connection:
            JobRepository(connection).expire_due(now=now)
        for _ in range(self._max_concurrency):
            with self._engine.begin() as connection:
                job = JobRepository(connection).lease_next(
                    lease_owner=self._lease_owner,
                    now=now,
                    lease_duration=self._lease_duration,
                )
            if job is None:
                break
            jobs.append(job)
        if not jobs:
            return 0
        with ThreadPoolExecutor(
            max_workers=len(jobs),
            thread_name_prefix="prediction-job",
        ) as executor:
            futures = [executor.submit(self._execute, job, now) for job in jobs]
            for future in futures:
                future.result()
        return len(jobs)

    def _execute(self, job: Mapping[str, Any], leased_at: datetime) -> None:
        job_id = job.get("id")
        if not isinstance(job_id, UUID):
            raise TypeError("leased job id must be UUID")
        handler = self._handlers.get(str(job.get("job_type")))
        if handler is None:
            with self._engine.begin() as connection:
                JobRepository(connection).quarantine(
                    job_id=job_id,
                    lease_owner=self._lease_owner,
                    error_code="unknown_job_type",
                    now=self._now(),
                )
            return
        try:
            finalized = handler.handle(job, now=leased_at)
        except RetryableJobError as error:
            completed_at = self._now()
            with self._engine.begin() as connection:
                queue = JobRepository(connection)
                attempt = int(job["attempt_no"])
                if attempt < self._timing.max_attempts:
                    changed = queue.schedule_retry(
                        job_id=job_id,
                        lease_owner=self._lease_owner,
                        retry_at=completed_at + self._timing.retry_delay(attempt),
                        error_code=error.error_code,
                        now=completed_at,
                    )
                else:
                    changed = queue.finish(
                        job_id=job_id,
                        lease_owner=self._lease_owner,
                        succeeded=False,
                        error_code=error.error_code,
                        completed_at=completed_at,
                    )
            if not changed:
                raise RuntimeError("job lease was lost while recording retry") from error
            return
        except TerminalJobError as error:
            with self._engine.begin() as connection:
                changed = JobRepository(connection).quarantine(
                    job_id=job_id,
                    lease_owner=self._lease_owner,
                    error_code=error.error_code,
                    now=self._now(),
                )
            if not changed:
                raise RuntimeError("job lease was lost while quarantining") from error
            return
        if finalized is True:
            return
        with self._engine.begin() as connection:
            changed = JobRepository(connection).finish(
                job_id=job_id,
                lease_owner=self._lease_owner,
                succeeded=True,
                error_code=None,
                completed_at=self._now(),
            )
        if not changed:
            raise RuntimeError("job lease was lost before completion")


def classify_schedule_change(
    current: ScheduleRevision,
    previous: ScheduleRevision | None,
) -> str | None:
    status = current.status.lower()
    if status == "cancelled":
        return "cancelled"
    if status == "postponed":
        return "postponed"
    if current.actual_start_at is not None and (
        previous is None
        or previous.actual_start_at is None
        or current.actual_start_at < previous.actual_start_at
    ):
        return "earlier_start"
    if previous is None or current.scheduled_start_at == previous.scheduled_start_at:
        return None
    if current.scheduled_start_at < previous.scheduled_start_at:
        return "earlier_start"
    return "rescheduled"


def _bucket(timestamp: datetime, interval: timedelta) -> datetime:
    seconds = int(interval.total_seconds())
    if seconds <= 0:
        raise ValueError("source sync interval must be positive")
    return datetime.fromtimestamp(
        int(timestamp.timestamp()) // seconds * seconds,
        tz=timestamp.tzinfo,
    )


def _integer(values: Mapping[str, object], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationalConfigError(f"timing.{key} must be an integer")
    return value


def _number(values: Mapping[str, object], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OperationalConfigError(f"timing.{key} must be a number")
    return float(value)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
