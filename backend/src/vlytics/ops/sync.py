"""Current-season sync planning, execution, and coverage reconciliation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from vlytics.mirror.backfill import BackfillRunner, BackfillScope

if TYPE_CHECKING:
    from vlytics.config import OperationalConfig


class SyncJobType(StrEnum):
    CURRENT_SCHEDULE = "mirror.current_schedule"
    FINAL_RESULT = "mirror.final_result"
    CORRECTION_RECHECK = "mirror.correction_recheck"


@dataclass(frozen=True)
class MatchSyncState:
    source_match_code: str
    scheduled_start_at: datetime
    status: str
    first_final_observed_at: datetime | None = None
    latest_result_observed_at: datetime | None = None


@dataclass(frozen=True)
class SyncJobSpec:
    job_key: str
    job_type: SyncJobType
    payload: Mapping[str, object]
    due_at: datetime
    deadline_at: datetime | None


class JobSink(Protocol):
    def enqueue(
        self,
        *,
        job_key: str,
        job_type: str,
        payload: Mapping[str, object],
        due_at: datetime,
        deadline_at: datetime | None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class SyncTiming:
    schedule_poll_interval: timedelta = timedelta(minutes=5)
    result_poll_interval: timedelta = timedelta(minutes=5)
    correction_interval: timedelta = timedelta(hours=6)
    correction_window: timedelta = timedelta(days=7)

    @classmethod
    def from_operational_config(cls, config: OperationalConfig) -> SyncTiming:
        results = config.values.get("results")
        if not isinstance(results, Mapping):
            raise ValueError("operational results configuration must be an object")

        def seconds(name: str) -> timedelta:
            value = results.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"operational results {name} must be a positive integer")
            return timedelta(seconds=value)

        window_hours = results.get("correction_recheck_window_hours")
        if isinstance(window_hours, bool) or not isinstance(window_hours, int) or window_hours <= 0:
            raise ValueError("operational results correction_recheck_window_hours must be positive")
        poll = seconds("poll_interval_seconds")
        return cls(
            schedule_poll_interval=poll,
            result_poll_interval=poll,
            correction_interval=seconds("correction_recheck_interval_seconds"),
            correction_window=timedelta(hours=window_hours),
        )


class CurrentSeasonSyncPlanner:
    def __init__(self, timing: SyncTiming | None = None) -> None:
        self._timing = timing or SyncTiming()

    def plan(
        self,
        scope: BackfillScope,
        matches: Iterable[MatchSyncState],
        *,
        now: datetime,
    ) -> tuple[SyncJobSpec, ...]:
        schedule_bucket = _bucket(now, self._timing.schedule_poll_interval)
        jobs = [
            SyncJobSpec(
                f"schedule:{scope.key}:{schedule_bucket.isoformat()}",
                SyncJobType.CURRENT_SCHEDULE,
                _scope_payload(scope),
                now,
                now + self._timing.schedule_poll_interval,
            )
        ]
        for match in matches:
            payload = {**_scope_payload(scope), "match_code": match.source_match_code}
            if match.first_final_observed_at is None:
                if match.scheduled_start_at <= now and match.status not in {
                    "cancelled",
                    "postponed",
                }:
                    bucket = _bucket(now, self._timing.result_poll_interval)
                    jobs.append(
                        SyncJobSpec(
                            f"result:{scope.key}:{match.source_match_code}:{bucket.isoformat()}",
                            SyncJobType.FINAL_RESULT,
                            payload,
                            now,
                            now + self._timing.result_poll_interval,
                        )
                    )
                continue
            window_end = match.first_final_observed_at + self._timing.correction_window
            last_observed = match.latest_result_observed_at or match.first_final_observed_at
            if now < window_end and last_observed + self._timing.correction_interval <= now:
                bucket = _bucket(now, self._timing.correction_interval)
                jobs.append(
                    SyncJobSpec(
                        f"correction:{scope.key}:{match.source_match_code}:{bucket.isoformat()}",
                        SyncJobType.CORRECTION_RECHECK,
                        payload,
                        now,
                        min(window_end, now + self._timing.correction_interval),
                    )
                )
        return tuple(jobs)

    def enqueue(
        self,
        sink: JobSink,
        scope: BackfillScope,
        matches: Iterable[MatchSyncState],
        *,
        now: datetime,
    ) -> int:
        jobs = self.plan(scope, matches, now=now)
        for job in jobs:
            sink.enqueue(
                job_key=job.job_key,
                job_type=job.job_type.value,
                payload=job.payload,
                due_at=job.due_at,
                deadline_at=job.deadline_at,
            )
        return len(jobs)


class DatabaseCurrentSeasonSyncTicker:
    """Plan schedule, final-result, and correction jobs for configured live scopes."""

    def __init__(
        self,
        engine: Engine,
        scopes: Iterable[BackfillScope],
        *,
        timing: SyncTiming | None = None,
    ) -> None:
        self._engine = engine
        self._scopes = tuple(scopes)
        if not self._scopes:
            raise ValueError("at least one source sync scope is required")
        if len({scope.key for scope in self._scopes}) != len(self._scopes):
            raise ValueError("source sync scopes must be unique")
        self._planner = CurrentSeasonSyncPlanner(timing)

    def run_once(self, *, now: datetime) -> int:
        from vlytics.ops.repositories.jobs import JobRepository

        planned = 0
        with self._engine.begin() as connection:
            jobs = JobRepository(connection)
            for scope in self._scopes:
                planned += self._planner.enqueue(
                    jobs,
                    scope,
                    self._matches(connection, scope),
                    now=now,
                )
        return planned

    @staticmethod
    def _matches(
        connection: Connection,
        scope: BackfillScope,
    ) -> tuple[MatchSyncState, ...]:
        rows = connection.execute(
            text(
                """
                WITH latest_schedule AS (
                    SELECT DISTINCT ON (revision.match_id)
                           revision.match_id, revision.scheduled_start_at,
                           revision.status
                    FROM mirror.match_revisions revision
                    ORDER BY revision.match_id, revision.revision DESC
                ), result_observations AS (
                    SELECT match_id,
                           min(observed_at) FILTER (
                               WHERE finality IN ('final', 'corrected')
                           ) AS first_final_observed_at,
                           max(observed_at) AS latest_result_observed_at
                    FROM mirror.result_revisions
                    GROUP BY match_id
                )
                SELECT match.source_match_code, schedule.scheduled_start_at,
                       schedule.status, result.first_final_observed_at,
                       result.latest_result_observed_at
                FROM mirror.matches match
                JOIN latest_schedule schedule ON schedule.match_id = match.id
                LEFT JOIN result_observations result ON result.match_id = match.id
                WHERE match.source=:source
                  AND match.source_group_code=:group_code
                  AND match.source_season_code=:season_code
                  AND match.source_competition_code=:competition_code
                ORDER BY schedule.scheduled_start_at, match.source_match_code
                """
            ),
            {
                "source": scope.source,
                "group_code": scope.group_code,
                "season_code": scope.season_code,
                "competition_code": scope.competition_code,
            },
        ).mappings()
        return tuple(
            MatchSyncState(
                source_match_code=str(row["source_match_code"]),
                scheduled_start_at=row["scheduled_start_at"],
                status=str(row["status"]),
                first_final_observed_at=row["first_final_observed_at"],
                latest_result_observed_at=row["latest_result_observed_at"],
            )
            for row in rows
        )


def _scope_payload(scope: BackfillScope) -> dict[str, object]:
    return {
        "source": scope.source,
        "group_code": scope.group_code,
        "season_code": scope.season_code,
        "competition_code": scope.competition_code,
    }


def scope_from_payload(payload: Mapping[str, object]) -> BackfillScope:
    values = tuple(
        payload.get(name) for name in ("source", "group_code", "season_code", "competition_code")
    )
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("sync job has an invalid scope payload")
    return BackfillScope(values[0], values[1], values[2], values[3])  # type: ignore[arg-type]


def _bucket(timestamp: datetime, interval: timedelta) -> datetime:
    seconds = int(interval.total_seconds())
    if seconds <= 0:
        raise ValueError("sync interval must be positive")
    return datetime.fromtimestamp(
        int(timestamp.timestamp()) // seconds * seconds,
        tz=timestamp.tzinfo,
    )


class CoverageStatus(StrEnum):
    LOADED = "loaded"
    MISSING = "missing"
    NOT_SUPPORTED = "not_supported"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class CoverageItem:
    source_match_code: str
    status: CoverageStatus
    reason: str


@dataclass(frozen=True)
class CoverageReport:
    scope: BackfillScope
    generated_at: datetime
    items: tuple[CoverageItem, ...]

    @property
    def counts(self) -> Mapping[CoverageStatus, int]:
        values = Counter(item.status for item in self.items)
        return {status: values[status] for status in CoverageStatus}

    def to_markdown(self) -> str:
        counts = self.counts
        expected = sum(item.status is not CoverageStatus.UNEXPECTED for item in self.items)
        lines = [
            f"## {self.scope.season_code} / {self.scope.competition_code}",
            "",
            f"생성 시각: `{self.generated_at.isoformat()}`",
            "",
            "| 예상 | 적재 | 누락 | 미지원 | 예상 외 |",
            "|---:|---:|---:|---:|---:|",
            (
                f"| {expected} | {counts[CoverageStatus.LOADED]} | "
                f"{counts[CoverageStatus.MISSING]} | "
                f"{counts[CoverageStatus.NOT_SUPPORTED]} | "
                f"{counts[CoverageStatus.UNEXPECTED]} |"
            ),
            "",
            "| 경기 코드 | 상태 | 사유 |",
            "|---|---|---|",
        ]
        lines.extend(
            f"| `{item.source_match_code}` | `{item.status.value}` | {item.reason} |"
            for item in self.items
            if item.status is not CoverageStatus.LOADED
        )
        if all(item.status is CoverageStatus.LOADED for item in self.items):
            lines.append("| - | `loaded` | 누락 없음 |")
        return "\n".join(lines) + "\n"


def reconcile_coverage(
    scope: BackfillScope,
    *,
    expected_match_codes: Iterable[str],
    loaded_match_codes: Iterable[str],
    unsupported: Mapping[str, str] | None = None,
    missing_reasons: Mapping[str, str] | None = None,
    generated_at: datetime,
) -> CoverageReport:
    expected = tuple(dict.fromkeys(expected_match_codes))
    loaded = set(loaded_match_codes)
    unsupported = unsupported or {}
    missing_reasons = missing_reasons or {}
    items: list[CoverageItem] = []
    for match_code in expected:
        if match_code in loaded:
            item = CoverageItem(match_code, CoverageStatus.LOADED, "source match loaded")
        elif match_code in unsupported:
            item = CoverageItem(match_code, CoverageStatus.NOT_SUPPORTED, unsupported[match_code])
        else:
            item = CoverageItem(
                match_code,
                CoverageStatus.MISSING,
                missing_reasons.get(match_code, "no accepted mirror match"),
            )
        items.append(item)
    for match_code in sorted(loaded.difference(expected)):
        items.append(
            CoverageItem(
                match_code,
                CoverageStatus.UNEXPECTED,
                "loaded match is absent from the source schedule",
            )
        )
    return CoverageReport(scope, generated_at, tuple(items))


class PostgresCoverageReader:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def loaded_match_codes(self, scope: BackfillScope) -> tuple[str, ...]:
        rows = self._connection.execute(
            text(
                """
                SELECT source_match_code
                FROM mirror.matches
                WHERE source=:source AND source_group_code=:group_code
                  AND source_season_code=:season_code
                  AND source_competition_code=:competition_code
                ORDER BY source_match_code
                """
            ),
            {
                "source": scope.source,
                "group_code": scope.group_code,
                "season_code": scope.season_code,
                "competition_code": scope.competition_code,
            },
        )
        return tuple(str(row[0]) for row in rows)


class JobQueue(Protocol):
    def lease_next(
        self, *, lease_owner: str, now: datetime, lease_duration: timedelta
    ) -> Mapping[str, Any] | None: ...

    def finish(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        succeeded: bool,
        error_code: str | None,
        completed_at: datetime,
    ) -> bool: ...

    def schedule_retry(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        retry_at: datetime,
        error_code: str,
        now: datetime,
    ) -> bool: ...

    def quarantine(
        self, *, job_id: UUID, lease_owner: str, error_code: str, now: datetime
    ) -> bool: ...


class SyncJobHandler(Protocol):
    def handle(self, job: Mapping[str, Any], *, now: datetime) -> None: ...


class SyncJobDispatcher:
    """Lease, execute, and transition one durable ops job."""

    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, SyncJobHandler],
        *,
        lease_owner: str,
        lease_duration: timedelta = timedelta(minutes=2),
        retry_delay: timedelta = timedelta(seconds=30),
        max_attempts: int = 3,
    ) -> None:
        self._queue = queue
        self._handlers = dict(handlers)
        self._lease_owner = lease_owner
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._max_attempts = max_attempts

    def run_once(self, *, now: datetime) -> int:
        job = self._queue.lease_next(
            lease_owner=self._lease_owner,
            now=now,
            lease_duration=self._lease_duration,
        )
        if job is None:
            return 0
        job_id = job["id"]
        if not isinstance(job_id, UUID):
            raise TypeError("leased job id must be UUID")
        handler = self._handlers.get(str(job["job_type"]))
        if handler is None:
            self._queue.quarantine(
                job_id=job_id,
                lease_owner=self._lease_owner,
                error_code="unknown_job_type",
                now=now,
            )
            return 1
        try:
            handler.handle(job, now=now)
        except Exception as error:
            attempt = int(job["attempt_no"])
            if attempt < self._max_attempts:
                self._queue.schedule_retry(
                    job_id=job_id,
                    lease_owner=self._lease_owner,
                    retry_at=now + self._retry_delay,
                    error_code=type(error).__name__,
                    now=now,
                )
            else:
                self._queue.finish(
                    job_id=job_id,
                    lease_owner=self._lease_owner,
                    succeeded=False,
                    error_code=type(error).__name__,
                    completed_at=now,
                )
            return 1
        self._queue.finish(
            job_id=job_id,
            lease_owner=self._lease_owner,
            succeeded=True,
            error_code=None,
            completed_at=now,
        )
        return 1


class BackfillSyncHandler:
    """Execute a leased source job and reconcile its resulting coverage."""

    def __init__(
        self,
        runner_factory: Callable[[Mapping[str, Any]], BackfillRunner],
        expected_codes: Callable[[BackfillScope], Iterable[str]],
        loaded_codes: Callable[[BackfillScope], Iterable[str]],
        report_sink: Callable[[CoverageReport], None],
    ) -> None:
        self._runner_factory = runner_factory
        self._expected_codes = expected_codes
        self._loaded_codes = loaded_codes
        self._report_sink = report_sink

    def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
        payload = job["payload"]
        if not isinstance(payload, Mapping):
            raise ValueError("sync job payload must be an object")
        scope = scope_from_payload(payload)
        runner = self._runner_factory(job)
        deadline = job.get("deadline_at")
        if deadline is not None and not isinstance(deadline, datetime):
            raise ValueError("sync job deadline must be a datetime")
        runner.sync_once(scope, deadline_at=deadline)
        self._report_sink(
            reconcile_coverage(
                scope,
                expected_match_codes=self._expected_codes(scope),
                loaded_match_codes=self._loaded_codes(scope),
                generated_at=now,
            )
        )
