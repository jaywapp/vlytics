"""Strict T-60 snapshot freezing and independent prediction persistence."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from sqlalchemy import Connection, Engine, text

from vlytics.engine.features import (
    FEATURE_VERSION,
    AvailabilityPolicy,
    FeatureSnapshot,
    FeatureSnapshotRepository,
)
from vlytics.engine.features.repository import StoredFeatureSnapshot
from vlytics.engine.prediction_validation import (
    JointScoreResolver,
    PredictionValidationContext,
    validate_prediction_output_v1,
)
from vlytics.engine.providers import (
    AttemptStatus,
    BudgetAccount,
    FailureCode,
    PostgresBudgetLedger,
    PredictionContextV1,
    PredictionProvider,
    ProviderBudgetLimits,
)
from vlytics.engine.providers.models import canonical_json_bytes, sha256_bytes
from vlytics.engine.repositories.attempts import (
    PredictionAttemptRecord,
    PredictionAttemptRepository,
)
from vlytics.engine.repositories.predictions import (
    PredictionEventRepository,
    PredictionProjectionRepository,
    PredictionRepository,
)
from vlytics.ops.repositories.jobs import JobRepository
from vlytics.ops.scheduler import (
    RetryableJobError,
    SchedulerTiming,
    TerminalJobError,
)


@dataclass(frozen=True)
class MatchExecutionContext:
    match_id: UUID
    schedule_revision_id: UUID
    scheduled_start_at: datetime
    actual_start_at: datetime | None
    status: str


class SnapshotFactory(Protocol):
    def build(
        self,
        schedule: MatchExecutionContext,
        *,
        cutoff_at: datetime,
        captured_at: datetime,
    ) -> FeatureSnapshot: ...


@dataclass(frozen=True)
class StatisticalPredictionResult:
    output: Mapping[str, Any]
    resolved_model_id: str

    def __post_init__(self) -> None:
        if not self.resolved_model_id.strip():
            raise ValueError("statistical resolved_model_id must not be blank")


class StatisticalPredictionRunner(Protocol):
    def predict(
        self,
        *,
        snapshot_id: UUID,
        snapshot: FeatureSnapshot,
    ) -> StatisticalPredictionResult: ...


@dataclass(frozen=True)
class ProviderBinding:
    variant_id: UUID
    provider: PredictionProvider


@dataclass(frozen=True)
class StatisticalBinding:
    variant_id: UUID
    runner: StatisticalPredictionRunner


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    ALREADY_SUCCEEDED = "already_succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    LATE_REJECTED = "late_rejected"


@dataclass(frozen=True)
class PredictionExecutionOutcome:
    status: ExecutionStatus
    prediction_id: UUID | None = None
    error_code: str | None = None
    job_finalized: bool = False


class LostJobLeaseError(RuntimeError):
    """Raised to roll back a publish transaction after lease ownership is lost."""


class PredictionOrchestrator:
    """Execute one variant without recalling another successful variant."""

    def __init__(
        self,
        connection: Connection | Engine,
        *,
        snapshot_factory: SnapshotFactory,
        statistical: Mapping[str, StatisticalBinding],
        providers: Mapping[str, ProviderBinding],
        budget: BudgetAccount,
        durable_budget_limits: Mapping[str, ProviderBudgetLimits] | None = None,
        timing: SchedulerTiming | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        prediction_schema_path: Path | None = None,
        joint_score_resolver: JointScoreResolver | None = None,
    ) -> None:
        self._database = connection
        self._snapshot_factory = snapshot_factory
        self._statistical = dict(statistical)
        self._providers = dict(providers)
        overlap = set(self._statistical) & set(self._providers)
        if overlap:
            raise ValueError(f"variant keys have multiple runners: {sorted(overlap)}")
        self._budget = budget
        self._durable_budget_limits = dict(durable_budget_limits or {})
        self._timing = timing or SchedulerTiming()
        self._now = now
        schema_path = prediction_schema_path or (
            Path(__file__).resolve().parents[4] / "contracts" / "prediction-v1.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            raise ValueError("prediction schema must be an object")
        Draft202012Validator.check_schema(schema)
        self._prediction_schema = cast(dict[str, Any], schema)
        self._joint_score_resolver = joint_score_resolver

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        if isinstance(self._database, Engine):
            with self._database.begin() as connection:
                yield connection
            return
        yield self._database

    def freeze_snapshot(
        self,
        job: Mapping[str, Any],
        *,
        now: datetime,
    ) -> StoredFeatureSnapshot:
        with self._transaction() as connection:
            schedule = self._load_schedule(connection, job, latest=True)
            cutoff = self._timing.cutoff_at(schedule.scheduled_start_at)
            deadline = self._timing.deadline_at(
                schedule.scheduled_start_at, schedule.actual_start_at
            )
            if now < cutoff:
                raise RetryableJobError("cutoff_not_reached")
            if now >= deadline:
                raise TerminalJobError("prediction_deadline_expired")
            if schedule.status.lower() in {"cancelled", "postponed"}:
                raise TerminalJobError(f"schedule_{schedule.status.lower()}")
            return FeatureSnapshotRepository(connection).freeze(
                match_id=schedule.match_id,
                schedule_revision_id=schedule.schedule_revision_id,
                cutoff_at=cutoff,
                feature_version=FEATURE_VERSION,
                availability_policy=AvailabilityPolicy.LIVE_PROSPECTIVE,
                build=lambda: self._snapshot_factory.build(
                    schedule,
                    cutoff_at=cutoff,
                    captured_at=now,
                ),
            )

    def execute(
        self,
        job: Mapping[str, Any],
        *,
        now: datetime,
    ) -> PredictionExecutionOutcome:
        with self._transaction() as connection:
            schedule = self._load_schedule(connection, job, latest=True)
        snapshot = self.freeze_snapshot(job, now=now)
        variant_key = _payload_string(job, "variant_key")
        stage = _payload_string(job, "stage")
        binding = self._binding(variant_key)
        variant_id = binding.variant_id
        job_variant = job.get("variant_id")
        if job_variant != variant_id:
            raise TerminalJobError("variant_identity_mismatch")

        with self._transaction() as connection:
            existing = PredictionRepository(connection).find(
                snapshot_id=snapshot.id,
                variant_id=variant_id,
                stage=stage,
            )
        if existing is not None:
            return PredictionExecutionOutcome(
                ExecutionStatus.ALREADY_SUCCEEDED,
                prediction_id=existing.id,
            )

        started_at = self._now()
        start_is_on_time = self._timing.start_is_on_time(started_at, schedule.scheduled_start_at)
        if isinstance(binding, ProviderBinding):
            provider_result = binding.provider.predict(
                PredictionContextV1.from_feature_snapshot(str(snapshot.id), snapshot.snapshot),
                timeout_ms=int(self._timing.request_timeout.total_seconds() * 1000),
                budget=self._budget_for(job, variant_key, binding, started_at),
            )
            completed_at = self._now()
            completion_is_on_time = self._timing.completion_is_on_time(
                completed_at,
                schedule.scheduled_start_at,
                schedule.actual_start_at,
            )
            status = (
                "late_rejected"
                if provider_result.status is AttemptStatus.SUCCEEDED
                and (not start_is_on_time or not completion_is_on_time)
                else None
            )
            record = PredictionAttemptRecord.from_result(
                provider_result,
                job_id=_job_id(job),
                snapshot_id=snapshot.id,
                variant_id=variant_id,
                started_at=started_at,
                completed_at=completed_at,
            )
            if status is not None:
                record = replace(
                    record,
                    status=status,
                    error_code="late_response",
                )
            if provider_result.status is not AttemptStatus.SUCCEEDED:
                with self._transaction() as connection:
                    PredictionAttemptRepository(connection).add(record)
                error_code = (
                    provider_result.failure_code.value
                    if provider_result.failure_code is not None
                    else "provider_failure"
                )
                execution_status = (
                    ExecutionStatus.RETRYABLE_FAILURE
                    if provider_result.failure_code
                    in {FailureCode.TIMEOUT, FailureCode.PROVIDER_ERROR}
                    else ExecutionStatus.TERMINAL_FAILURE
                )
                return PredictionExecutionOutcome(execution_status, error_code=error_code)
            assert provider_result.output is not None
            assert provider_result.resolved_model_id is not None
            output: Mapping[str, Any] = provider_result.output
            resolved_model_id: str = provider_result.resolved_model_id
        else:
            statistical_result = binding.runner.predict(
                snapshot_id=snapshot.id,
                snapshot=snapshot.snapshot,
            )
            completed_at = self._now()
            completion_is_on_time = self._timing.completion_is_on_time(
                completed_at,
                schedule.scheduled_start_at,
                schedule.actual_start_at,
            )
            output = statistical_result.output
            resolved_model_id = statistical_result.resolved_model_id
            response_hash = sha256_bytes(canonical_json_bytes(output))
            record = PredictionAttemptRecord(
                job_id=_job_id(job),
                snapshot_id=snapshot.id,
                variant_id=variant_id,
                started_at=started_at,
                completed_at=completed_at,
                status=(
                    "succeeded" if start_is_on_time and completion_is_on_time else "late_rejected"
                ),
                error_code=(
                    None if start_is_on_time and completion_is_on_time else "late_response"
                ),
                request_hash=snapshot.snapshot.sha256,
                response_hash=response_hash,
                usage_json={"provider": "statistical"},
                latency_ms=max(0, round((completed_at - started_at).total_seconds() * 1000)),
            )

        if not start_is_on_time or not completion_is_on_time:
            with self._transaction() as connection:
                PredictionAttemptRepository(connection).add(record)
                self._audit_late_discard(
                    connection,
                    job=job,
                    schedule=schedule,
                    snapshot=snapshot,
                    variant_key=variant_key,
                    started_at=started_at,
                    completed_at=completed_at,
                    reason="strict_t60_deadline",
                )
            return PredictionExecutionOutcome(
                ExecutionStatus.LATE_REJECTED,
                error_code="late_response",
            )

        validate_prediction_output_v1(
            output,
            self._prediction_schema,
            context=PredictionValidationContext(
                producer_variant_id=variant_key,
                input_snapshot_id=str(snapshot.id),
                joint_score_resolver=self._joint_score_resolver,
            ),
        )
        try:
            with self._transaction() as connection:
                current = self._publishable_schedule(
                    connection,
                    job=job,
                    completed_at=completed_at,
                )
                if current is None:
                    raise LostJobLeaseError("schedule_or_lease_changed")
                attempts = PredictionAttemptRepository(connection)
                attempts.add(record)
                stored = PredictionRepository(connection).add(
                    match_id=current.match_id,
                    schedule_revision_id=current.schedule_revision_id,
                    snapshot_id=snapshot.id,
                    variant_id=variant_id,
                    stage=stage,
                    input_cutoff_at=snapshot.snapshot.cutoff_at,
                    started_at=started_at,
                    generated_at=completed_at,
                    resolved_model_id=resolved_model_id,
                    output=output,
                )
                event_id = PredictionEventRepository(connection).add_once(
                    prediction_id=stored.id,
                    event_type="published",
                    reason="strict_t60_success",
                    occurred_at=completed_at,
                    observed_at=completed_at,
                    schedule_revision_id=current.schedule_revision_id,
                )
                PredictionProjectionRepository(connection).apply_event(
                    prediction_id=stored.id,
                    event_id=event_id,
                    current_status="published",
                    refreshed_at=completed_at,
                )
                lease_owner = _lease_owner(job)
                if not JobRepository(connection).finish(
                    job_id=_job_id(job),
                    lease_owner=lease_owner,
                    succeeded=True,
                    error_code=None,
                    completed_at=completed_at,
                ):
                    raise LostJobLeaseError("lost_job_lease")
            return PredictionExecutionOutcome(
                ExecutionStatus.SUCCEEDED if stored.created else ExecutionStatus.ALREADY_SUCCEEDED,
                prediction_id=stored.id,
                job_finalized=True,
            )
        except LostJobLeaseError as error:
            late_record = replace(record, status="late_rejected", error_code=str(error))
            with self._transaction() as connection:
                PredictionAttemptRepository(connection).add(late_record)
                self._audit_late_discard(
                    connection,
                    job=job,
                    schedule=schedule,
                    snapshot=snapshot,
                    variant_key=variant_key,
                    started_at=started_at,
                    completed_at=completed_at,
                    reason=str(error),
                )
            return PredictionExecutionOutcome(
                ExecutionStatus.LATE_REJECTED,
                error_code=str(error),
            )

    def _binding(self, variant_key: str) -> ProviderBinding | StatisticalBinding:
        provider = self._providers.get(variant_key)
        statistical = self._statistical.get(variant_key)
        binding = provider or statistical
        if binding is None:
            raise TerminalJobError("unknown_prediction_variant")
        return binding

    def _load_schedule(
        self,
        connection: Connection,
        job: Mapping[str, Any],
        *,
        latest: bool,
    ) -> MatchExecutionContext:
        match_id = _payload_uuid(job, "match_id")
        schedule_revision_id = _payload_uuid(job, "schedule_revision_id")
        row = (
            connection.execute(
                text(
                    """
                    SELECT match_id, id, scheduled_start_at, actual_start_at, status
                    FROM mirror.match_revisions
                    WHERE id = :schedule_revision_id AND match_id = :match_id
                      AND (
                          NOT :latest
                          OR revision = (
                              SELECT max(latest_revision.revision)
                              FROM mirror.match_revisions AS latest_revision
                              WHERE latest_revision.match_id = :match_id
                          )
                      )
                    """
                ),
                {
                    "match_id": match_id,
                    "schedule_revision_id": schedule_revision_id,
                    "latest": latest,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TerminalJobError("schedule_revision_not_found")
        schedule = MatchExecutionContext(
            match_id=cast(UUID, row["match_id"]),
            schedule_revision_id=cast(UUID, row["id"]),
            scheduled_start_at=cast(datetime, row["scheduled_start_at"]),
            actual_start_at=cast(datetime | None, row["actual_start_at"]),
            status=str(row["status"]),
        )
        payload = _payload(job)
        payload_cutoff = _parse_datetime(payload.get("cutoff_at"), "cutoff_at")
        if payload_cutoff != self._timing.cutoff_at(schedule.scheduled_start_at):
            raise TerminalJobError("schedule_payload_changed")
        return schedule

    def _publishable_schedule(
        self,
        connection: Connection,
        *,
        job: Mapping[str, Any],
        completed_at: datetime,
    ) -> MatchExecutionContext | None:
        connection.execute(
            text(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended('match-schedule:' || CAST(:match_id AS text), 0)
                )
                """
            ),
            {"match_id": _payload_uuid(job, "match_id")},
        )
        if not JobRepository(connection).owns_active_lease(
            job_id=_job_id(job),
            lease_owner=_lease_owner(job),
            now=completed_at,
        ):
            return None
        try:
            schedule = self._load_schedule(connection, job, latest=True)
        except TerminalJobError:
            return None
        if schedule.status.lower() in {"cancelled", "postponed"}:
            return None
        if not self._timing.completion_is_on_time(
            completed_at,
            schedule.scheduled_start_at,
            schedule.actual_start_at,
        ):
            return None
        return schedule

    def _budget_for(
        self,
        job: Mapping[str, Any],
        variant_key: str,
        binding: ProviderBinding,
        reserved_at: datetime,
    ) -> BudgetAccount:
        limits = self._durable_budget_limits.get(variant_key)
        if limits is None:
            return self._budget
        if not isinstance(self._database, Engine):
            raise TerminalJobError("durable_budget_requires_engine")
        attempt_no = job.get("attempt_no")
        if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no <= 0:
            raise TerminalJobError("invalid_job_attempt")
        return PostgresBudgetLedger(
            self._database,
            reservation_key=f"{_job_id(job)}:{attempt_no}:{binding.provider.provider_name.value}",
            provider=binding.provider.provider_name.value,
            job_id=_job_id(job),
            limits=limits,
            reserved_at=reserved_at,
        )

    def _audit_late_discard(
        self,
        connection: Connection,
        *,
        job: Mapping[str, Any],
        schedule: MatchExecutionContext,
        snapshot: StoredFeatureSnapshot,
        variant_key: str,
        started_at: datetime,
        completed_at: datetime,
        reason: str,
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO ops.audit_events (
                    actor, action, object_type, object_id, reason,
                    correlation_id, occurred_at, metadata
                ) VALUES (
                    'prediction-worker', 'late_response_discarded',
                    'prediction_attempt', :object_id, :reason,
                    :correlation_id, :occurred_at, CAST(:metadata AS jsonb)
                )
                """
            ),
            {
                "object_id": f"{snapshot.id}:{variant_key}",
                "correlation_id": _job_id(job),
                "occurred_at": completed_at,
                "reason": reason,
                "metadata": json.dumps(
                    {
                        "actual_start_at": (
                            schedule.actual_start_at.isoformat()
                            if schedule.actual_start_at is not None
                            else None
                        ),
                        "completed_at": completed_at.isoformat(),
                        "scheduled_start_at": schedule.scheduled_start_at.isoformat(),
                        "started_at": started_at.isoformat(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )


class FreezeSnapshotJobHandler:
    def __init__(self, orchestrator: PredictionOrchestrator) -> None:
        self._orchestrator = orchestrator

    def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
        self._orchestrator.freeze_snapshot(job, now=now)


class PredictionJobHandler:
    def __init__(self, orchestrator: PredictionOrchestrator) -> None:
        self._orchestrator = orchestrator

    def handle(self, job: Mapping[str, Any], *, now: datetime) -> bool:
        outcome = self._orchestrator.execute(job, now=now)
        if outcome.status is ExecutionStatus.RETRYABLE_FAILURE:
            raise RetryableJobError(outcome.error_code or "prediction_retryable_failure")
        if outcome.status is ExecutionStatus.TERMINAL_FAILURE:
            raise TerminalJobError(outcome.error_code or "prediction_terminal_failure")
        return outcome.job_finalized


def scheduler_handlers(
    orchestrator: PredictionOrchestrator,
) -> dict[str, FreezeSnapshotJobHandler | PredictionJobHandler]:
    """Return the worker handler map for the two engine-owned job types."""

    return {
        "engine.freeze_snapshot": FreezeSnapshotJobHandler(orchestrator),
        "engine.run_prediction": PredictionJobHandler(orchestrator),
    }


def _job_id(job: Mapping[str, Any]) -> UUID:
    value = job.get("id")
    if not isinstance(value, UUID):
        raise TerminalJobError("invalid_job_id")
    return value


def _lease_owner(job: Mapping[str, Any]) -> str:
    value = job.get("lease_owner")
    if not isinstance(value, str) or not value.strip():
        raise TerminalJobError("invalid_lease_owner")
    return value


def _payload(job: Mapping[str, Any]) -> Mapping[str, Any]:
    value = job.get("payload")
    if not isinstance(value, Mapping):
        raise TerminalJobError("invalid_job_payload")
    return value


def _payload_string(job: Mapping[str, Any], key: str) -> str:
    value = _payload(job).get(key)
    if not isinstance(value, str) or not value:
        raise TerminalJobError(f"invalid_{key}")
    return value


def _payload_uuid(job: Mapping[str, Any], key: str) -> UUID:
    try:
        return UUID(_payload_string(job, key))
    except ValueError as error:
        raise TerminalJobError(f"invalid_{key}") from error


def _parse_datetime(value: object, key: str) -> datetime:
    if not isinstance(value, str):
        raise TerminalJobError(f"invalid_{key}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise TerminalJobError(f"invalid_{key}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TerminalJobError(f"invalid_{key}")
    return parsed
