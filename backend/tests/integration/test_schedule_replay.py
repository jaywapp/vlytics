"""Replay tests for strict T-60 scheduling and immutable prediction execution."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from vlytics.config import OperationalConfig, OperationalConfigError, Settings
from vlytics.engine.features import (
    FEATURE_VERSION,
    AvailabilityPolicy,
    FeatureLineage,
    FeatureSnapshot,
    FeatureStatus,
    FeatureValue,
    LineupStatus,
    MissingReason,
    compute_snapshot_sha256,
)
from vlytics.engine.features.definitions import MATCHUP_FEATURE_KEYS, TEAM_FEATURE_KEYS
from vlytics.engine.orchestrator import (
    ExecutionStatus,
    MatchExecutionContext,
    PredictionOrchestrator,
    ProviderBinding,
    StatisticalBinding,
    StatisticalPredictionResult,
    scheduler_handlers,
)
from vlytics.engine.providers import (
    AnthropicPredictionProvider,
    BudgetLedger,
    FakeProviderTransport,
    GooglePredictionProvider,
    OpenAIPredictionProvider,
    PostgresBudgetLedger,
    PredictionProvider,
    ProviderBudgetLimits,
    ProviderName,
    ProviderVariant,
    VersionPolicy,
)
from vlytics.engine.providers.base import PROMPT_TEMPLATE_HASH
from vlytics.ops.repositories.jobs import JobRepository
from vlytics.ops.scheduler import (
    DurableScheduler,
    PredictionJobDispatcher,
    PredictionKind,
    PredictionVariantSpec,
    ScheduleLifecycleCoordinator,
    SchedulePlanner,
    ScheduleRevision,
    SchedulerTiming,
    classify_schedule_change,
    validate_live_scheduler_activation,
)
from vlytics.worker import WorkerRuntimeComponents, main

NOW = datetime(2026, 9, 20, 3, tzinfo=UTC)
PREDICTION_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "prediction-v1.schema.json").read_text(
        encoding="utf-8"
    )
)
OUTCOMES = ("3:0", "3:1", "3:2", "2:3", "1:3", "0:3")


class PostgresRoleUrls(Protocol):
    collector: str
    engine: str


@dataclass
class FakeClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def advance(self, value: timedelta) -> None:
        self.current += value


def _feature_value(key: str) -> FeatureValue:
    return FeatureValue(
        definition=key,
        status=FeatureStatus.UNKNOWN,
        value=None,
        numerator=None,
        denominator=None,
        sample_size=0,
        missing_reason=MissingReason.MISSING_INPUT,
        lineage=FeatureLineage(),
    )


class SnapshotFactory:
    def __init__(self) -> None:
        self.calls = 0

    def build(
        self,
        schedule: MatchExecutionContext,
        *,
        cutoff_at: datetime,
        captured_at: datetime,
    ) -> FeatureSnapshot:
        self.calls += 1
        snapshot = FeatureSnapshot(
            feature_version=FEATURE_VERSION,
            availability_policy=AvailabilityPolicy.LIVE_PROSPECTIVE,
            target_match_id=str(schedule.match_id),
            schedule_revision_id=str(schedule.schedule_revision_id),
            cutoff_at=cutoff_at,
            captured_at=captured_at,
            lineup_status=LineupStatus.UNKNOWN,
            team_features={
                side: {key: _feature_value(key) for key in TEAM_FEATURE_KEYS}
                for side in ("home", "away")
            },
            matchup_features={key: _feature_value(key) for key in MATCHUP_FEATURE_KEYS},
            players={},
            lineage=FeatureLineage(raw_snapshot_sha256=(f"{self.calls:064x}",)),
            sha256="0" * 64,
        )
        return replace(snapshot, sha256=compute_snapshot_sha256(snapshot))


class StatisticalRunner:
    def __init__(self, variant_key: str, clock: FakeClock) -> None:
        self.variant_key = variant_key
        self.clock = clock
        self.calls = 0
        self.advance_on_call = timedelta()

    def predict(
        self,
        *,
        snapshot_id: UUID,
        snapshot: FeatureSnapshot,
    ) -> StatisticalPredictionResult:
        self.calls += 1
        self.clock.advance(self.advance_on_call)
        probabilities = (0.2, 0.15, 0.15, 0.15, 0.15, 0.2)
        provenance = {
            "producer_variant_id": self.variant_key,
            "distribution_version": "synthetic-set-v1",
            "probability_source": "statistical_derived",
            "derivation_method": "independent_set_distribution_marginalized_to_winner",
            "input_snapshot_id": str(snapshot_id),
            "upstream_prediction_id": "synthetic-elo-prediction",
            "upstream_model_version": "synthetic-elo-v1",
            "upstream_config_id": "a" * 64,
            "fitted_parameter_artifact_id": "b" * 64,
            "training_cohort": {
                "division": "men",
                "availability_policy": "live_prospective",
                "competition": "synthetic",
                "stage": "regular",
                "timing_eligibility": "on_time",
                "result_finality_policy": "final_only",
                "input_version": "synthetic-v1",
                "franchise_mapping_version": "synthetic-v1",
            },
            "training_as_of": snapshot.cutoff_at.isoformat(),
            "training_result_manifest_sha256": "c" * 64,
        }
        output = {
            "schema_version": "prediction-v1",
            "producer_variant_id": self.variant_key,
            "model_version": "synthetic-statistical-v1",
            "distribution_version": "synthetic-set-v1",
            "config_id": "d" * 64,
            "random_seed": 0,
            "division": "men",
            "capabilities": ["winner", "set_score"],
            "home_win_probability": 0.5,
            "set_score_probabilities": [
                {"outcome": outcome, "probability": probability}
                for outcome, probability in zip(OUTCOMES, probabilities, strict=True)
            ],
            "target_provenance": {
                "winner": dict(provenance),
                "set_score": dict(provenance),
            },
            "rationale": "Synthetic deterministic baseline.",
            "risk_factors": ["synthetic_fixture"],
        }
        return StatisticalPredictionResult(output, "synthetic-statistical-v1")


def _variant(provider: ProviderName) -> ProviderVariant:
    return ProviderVariant(
        variant_id=f"{provider.value}-independent-v1",
        provider=provider,
        requested_model_id=f"{provider.value}-alias",
        pinned_model_version=f"{provider.value}-pinned-v1",
        version_policy=VersionPolicy.VERIFY_RESOLVED_MODEL_ID,
        prompt_version="independent-v1",
        prompt_hash=PROMPT_TEMPLATE_HASH,
        distribution_version="ai-direct-v1",
        max_input_tokens=100_000,
        max_output_tokens=1_000,
        input_cost_per_million=Decimal("1"),
        output_cost_per_million=Decimal("1"),
        enabled=True,
        op003_resolved=True,
    )


def _provider_response(provider: ProviderName, variant: ProviderVariant) -> bytes:
    core = {
        "home_win_probability": 0.5,
        "set_score_probabilities": [
            {"outcome": outcome, "probability": probability}
            for outcome, probability in zip(
                OUTCOMES,
                (0.2, 0.15, 0.15, 0.15, 0.15, 0.2),
                strict=True,
            )
        ],
        "rationale": "Synthetic provider result.",
        "risk_factors": ["synthetic_fixture"],
    }
    output_text = json.dumps(core)
    if provider is ProviderName.OPENAI:
        body = {
            "id": "openai-request",
            "model": variant.pinned_model_version,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": output_text}],
                }
            ],
        }
    elif provider is ProviderName.ANTHROPIC:
        body = {
            "id": "anthropic-request",
            "model": variant.pinned_model_version,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": output_text}],
        }
    else:
        body = {
            "responseId": "google-request",
            "modelVersion": variant.pinned_model_version,
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10},
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": output_text}]}}],
        }
    return json.dumps(body, separators=(",", ":")).encode()


def _provider_adapter(
    provider: ProviderName,
    variant: ProviderVariant,
    transport: FakeProviderTransport,
) -> PredictionProvider:
    if provider is ProviderName.OPENAI:
        return OpenAIPredictionProvider(variant, transport, PREDICTION_SCHEMA)
    if provider is ProviderName.ANTHROPIC:
        return AnthropicPredictionProvider(variant, transport, PREDICTION_SCHEMA)
    return GooglePredictionProvider(variant, transport, PREDICTION_SCHEMA)


def _schedule(match_id: UUID | None = None, revision_id: UUID | None = None) -> ScheduleRevision:
    return ScheduleRevision(
        match_id=match_id or uuid4(),
        id=revision_id or uuid4(),
        revision=1,
        scheduled_start_at=NOW + timedelta(hours=2),
        actual_start_at=None,
        status="scheduled",
        observed_at=NOW,
        source_scope={
            "source": "synthetic",
            "group_code": "001",
            "season_code": "2026",
            "competition_code": "regular",
        },
    )


def test_planner_handles_zero_and_multiple_matches_without_t10_jobs() -> None:
    variants = (
        PredictionVariantSpec(uuid4(), "statistical-v1", PredictionKind.STATISTICAL),
        *(
            PredictionVariantSpec(uuid4(), provider.value, PredictionKind.PROVIDER)
            for provider in ProviderName
        ),
    )
    planner = SchedulePlanner()
    assert planner.plan((), variants, now=NOW) == ()

    jobs = planner.plan((_schedule(), _schedule()), variants, now=NOW)
    assert len(jobs) == 12
    assert len({job.job_key for job in jobs}) == len(jobs)
    assert sum(job.stage == "source_sync" for job in jobs) == 2
    assert sum(job.stage == "freeze" for job in jobs) == 2
    assert sum(job.stage == "pregame" for job in jobs) == 8
    assert all("t10" not in job.job_key.lower() for job in jobs)


class MemoryJobSink:
    def __init__(self) -> None:
        self.rows: dict[str, Mapping[str, Any]] = {}

    def enqueue(self, **values: Any) -> Mapping[str, Any]:
        self.rows.setdefault(str(values["job_key"]), dict(values))
        return self.rows[str(values["job_key"])]


def test_duplicate_schedule_replay_keeps_one_job_per_key() -> None:
    sink = MemoryJobSink()
    scheduler = DurableScheduler(sink, SchedulePlanner())  # type: ignore[arg-type]
    schedule = _schedule()
    variants = (PredictionVariantSpec(uuid4(), "statistical-v1", PredictionKind.STATISTICAL),)

    assert scheduler.enqueue((schedule,), variants, now=NOW) == 3
    assert scheduler.enqueue((schedule,), variants, now=NOW) == 3
    assert len(sink.rows) == 3


def test_postponement_cancellation_and_earlier_start_are_distinct_events() -> None:
    original = _schedule()
    postponed = replace(
        original,
        id=uuid4(),
        revision=2,
        status="postponed",
        observed_at=NOW + timedelta(minutes=1),
    )
    cancelled = replace(
        original,
        id=uuid4(),
        revision=2,
        status="cancelled",
        observed_at=NOW + timedelta(minutes=1),
    )
    earlier = replace(
        original,
        id=uuid4(),
        revision=2,
        scheduled_start_at=original.scheduled_start_at - timedelta(minutes=20),
        observed_at=NOW + timedelta(minutes=1),
    )
    later = replace(
        original,
        id=uuid4(),
        revision=2,
        scheduled_start_at=original.scheduled_start_at + timedelta(hours=1),
        observed_at=NOW + timedelta(minutes=1),
    )

    assert classify_schedule_change(postponed, original) == "postponed"
    assert classify_schedule_change(cancelled, original) == "cancelled"
    assert classify_schedule_change(earlier, original) == "earlier_start"
    assert classify_schedule_change(later, original) == "rescheduled"


def test_live_scheduler_stays_fail_closed_without_exact_dry_run_evidence(
    tmp_path: Path,
) -> None:
    config = OperationalConfig(
        values={"live_operations_enabled": True},
        source_path=tmp_path / "live.toml",
        schema_path=tmp_path / "schema.json",
    )
    with pytest.raises(OperationalConfigError, match="dry-run evidence"):
        validate_live_scheduler_activation(config, None)


def _insert_match(
    postgres_role_urls: PostgresRoleUrls,
    *,
    scheduled_start_at: datetime,
) -> tuple[UUID, UUID]:
    collector = create_engine(postgres_role_urls.collector)
    match_id = uuid4()
    revision_id = uuid4()
    suffix = uuid4().hex
    raw_id = uuid4()
    season_id = uuid4()
    competition_id = uuid4()
    home_id = uuid4()
    away_id = uuid4()
    with collector.begin() as connection:
        body = b"{}"
        connection.execute(
            text(
                """
                INSERT INTO mirror.raw_snapshots (
                    id, source, source_group_code, request_fingerprint, redacted_url,
                    requested_at, received_at, status_code, body_bytes, sha256, parser_version
                ) VALUES (
                    :id, 'synthetic', '001', :fingerprint, '/synthetic',
                    :now, :now, 200, :body, :sha256, 'synthetic-v1'
                )
                """
            ),
            {
                "id": raw_id,
                "fingerprint": suffix,
                "now": NOW,
                "body": body,
                "sha256": hashlib.sha256(body).hexdigest(),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO mirror.seasons (
                    id, source, source_group_code, source_season_code, label,
                    observed_at, raw_snapshot_id
                ) VALUES (:id, 'synthetic', '001', :code, 'Synthetic', :now, :raw_id)
                """
            ),
            {"id": season_id, "code": suffix, "now": NOW, "raw_id": raw_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO mirror.competitions (
                    id, source, source_group_code, season_id, source_competition_code,
                    division, stage, label, mapping_version, observed_at, raw_snapshot_id
                ) VALUES (
                    :id, 'synthetic', '001', :season_id, :code, 'men', 'regular',
                    'Synthetic', 'synthetic-v1', :now, :raw_id
                )
                """
            ),
            {
                "id": competition_id,
                "season_id": season_id,
                "code": suffix,
                "now": NOW,
                "raw_id": raw_id,
            },
        )
        for team_id, side in ((home_id, "home"), (away_id, "away")):
            connection.execute(
                text(
                    """
                    INSERT INTO mirror.team_identities (
                        id, source, source_team_code, season_id, display_name,
                        observed_at, mapping_version, raw_snapshot_id
                    ) VALUES (
                        :id, 'synthetic', :code, :season_id, :name,
                        :now, 'synthetic-v1', :raw_id
                    )
                    """
                ),
                {
                    "id": team_id,
                    "code": f"{side}-{suffix}",
                    "season_id": season_id,
                    "name": side,
                    "now": NOW,
                    "raw_id": raw_id,
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO mirror.matches (
                    id, source, source_group_code, source_season_code,
                    source_competition_code, source_match_code, season_id,
                    competition_id, first_observed_at, raw_snapshot_id
                ) VALUES (
                    :id, 'synthetic', '001', :code, :code, :code,
                    :season_id, :competition_id, :now, :raw_id
                )
                """
            ),
            {
                "id": match_id,
                "code": suffix,
                "season_id": season_id,
                "competition_id": competition_id,
                "now": NOW,
                "raw_id": raw_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO mirror.match_revisions (
                    id, match_id, revision, home_team_id, away_team_id,
                    scheduled_start_at, status, observed_at, raw_snapshot_id
                ) VALUES (
                    :id, :match_id, 1, :home_id, :away_id,
                    :start, 'scheduled', :now, :raw_id
                )
                """
            ),
            {
                "id": revision_id,
                "match_id": match_id,
                "home_id": home_id,
                "away_id": away_id,
                "start": scheduled_start_at,
                "now": NOW,
                "raw_id": raw_id,
            },
        )
    return match_id, revision_id


def _insert_match_revision(
    postgres_role_urls: PostgresRoleUrls,
    *,
    previous_revision_id: UUID,
    scheduled_start_at: datetime,
    status: str,
    observed_at: datetime,
    actual_start_at: datetime | None = None,
) -> UUID:
    revision_id = uuid4()
    collector = create_engine(postgres_role_urls.collector)
    with collector.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mirror.match_revisions (
                    id, match_id, revision, home_team_id, away_team_id, venue_id,
                    scheduled_start_at, actual_start_at, status, observed_at,
                    raw_snapshot_id
                )
                SELECT :id, match_id, revision + 1, home_team_id, away_team_id, venue_id,
                       :scheduled_start_at, :actual_start_at, :status, :observed_at,
                       raw_snapshot_id
                FROM mirror.match_revisions
                WHERE id = :previous_revision_id
                """
            ),
            {
                "id": revision_id,
                "previous_revision_id": previous_revision_id,
                "scheduled_start_at": scheduled_start_at,
                "actual_start_at": actual_start_at,
                "status": status,
                "observed_at": observed_at,
            },
        )
    return revision_id


def _insert_variant(
    connection: Any,
    *,
    provider: str,
    key: str,
    model: str,
    prompt_hash: str,
) -> UUID:
    hyperparameters = json.dumps({"key": key})
    existing = connection.execute(
        text(
            """
            SELECT id
            FROM engine.model_variants
            WHERE provider = :provider
              AND requested_model = :model
              AND pinned_model_version = :model
              AND prompt_hash = :prompt_hash
              AND feature_version = :feature_version
              AND output_schema_version = 'prediction-v1'
              AND hyperparameters = CAST(:hyperparameters AS jsonb)
              AND experiment_group IS NULL
            """
        ),
        {
            "provider": provider,
            "model": model,
            "prompt_hash": prompt_hash,
            "feature_version": FEATURE_VERSION,
            "hyperparameters": hyperparameters,
        },
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    variant_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO engine.model_variants (
                id, provider, requested_model, pinned_model_version, prompt_version,
                prompt_hash, feature_version, output_schema_version, hyperparameters
            ) VALUES (
                :id, :provider, :model, :model, 'synthetic-v1',
                :prompt_hash, :feature_version, 'prediction-v1',
                CAST(:hyperparameters AS jsonb)
            )
            """
        ),
        {
            "id": variant_id,
            "provider": provider,
            "model": model,
            "prompt_hash": prompt_hash,
            "feature_version": FEATURE_VERSION,
            "hyperparameters": hyperparameters,
        },
    )
    return variant_id


def _lease_next(
    engine: Any,
    *,
    now: datetime,
    owner: str = "synthetic-worker",
) -> Mapping[str, Any]:
    with engine.begin() as connection:
        job = JobRepository(connection).lease_next(
            lease_owner=owner,
            now=now,
            lease_duration=timedelta(minutes=2),
        )
    assert job is not None
    return job


def test_concurrent_claim_and_expired_lease_restart_recovery(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = create_engine(postgres_role_urls.engine)
    key = f"lease-recovery-{uuid4()}"
    with engine.begin() as connection:
        JobRepository(connection).enqueue(
            job_key=key,
            job_type="synthetic",
            payload={},
            due_at=NOW,
            deadline_at=NOW + timedelta(minutes=5),
        )

    first_connection = engine.connect()
    second_connection = engine.connect()
    first_transaction = first_connection.begin()
    second_transaction = second_connection.begin()
    try:
        first = JobRepository(first_connection).lease_next(
            lease_owner="worker-1",
            now=NOW,
            lease_duration=timedelta(seconds=30),
        )
        second = JobRepository(second_connection).lease_next(
            lease_owner="worker-2",
            now=NOW,
            lease_duration=timedelta(seconds=30),
        )
        assert first is not None
        assert second is None
        first_transaction.commit()
        second_transaction.commit()
    finally:
        first_connection.close()
        second_connection.close()

    with engine.begin() as connection:
        recovered = JobRepository(connection).lease_next(
            lease_owner="worker-3",
            now=NOW + timedelta(seconds=30),
            lease_duration=timedelta(seconds=30),
        )
        assert recovered is not None
        assert recovered["attempt_no"] == 2
        attempts = connection.execute(
            text(
                """
                SELECT attempt_no, outcome, error_code
                FROM ops.job_attempts
                WHERE job_id = :job_id
                """
            ),
            {"job_id": recovered["id"]},
        ).all()
        assert [tuple(row) for row in attempts] == [(1, "abandoned", "lease_expired")]


def test_replay_freezes_one_snapshot_runs_four_independent_variants_and_never_recalls_success(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    scheduled_start = NOW + timedelta(hours=1)
    cutoff = scheduled_start - timedelta(minutes=60)
    match_id, revision_id = _insert_match(
        postgres_role_urls,
        scheduled_start_at=scheduled_start,
    )
    engine = create_engine(postgres_role_urls.engine)
    clock = FakeClock(cutoff)
    factory = SnapshotFactory()
    statistical_runner = StatisticalRunner("statistical-v1", clock)
    transports: dict[str, FakeProviderTransport] = {}

    with engine.begin() as connection:
        statistical_id = _insert_variant(
            connection,
            provider="statistical",
            key="statistical-v1",
            model="synthetic-statistical-v1",
            prompt_hash="e" * 64,
        )
        statistical = {"statistical-v1": StatisticalBinding(statistical_id, statistical_runner)}
        providers: dict[str, ProviderBinding] = {}
        for provider_name in ProviderName:
            variant = _variant(provider_name)
            variant_id = _insert_variant(
                connection,
                provider=provider_name.value,
                key=variant.variant_id,
                model=variant.requested_model_id,
                prompt_hash=PROMPT_TEMPLATE_HASH,
            )
            transport = FakeProviderTransport(
                response_body=_provider_response(provider_name, variant)
            )
            transports[variant.variant_id] = transport
            providers[variant.variant_id] = ProviderBinding(
                variant_id,
                _provider_adapter(provider_name, variant, transport),
            )
        queue = JobRepository(connection)
        variant_jobs = [
            ("statistical-v1", statistical_id, PredictionKind.STATISTICAL),
            *[
                (key, binding.variant_id, PredictionKind.PROVIDER)
                for key, binding in providers.items()
            ],
        ]
        for key, variant_id, kind in variant_jobs:
            queue.enqueue(
                job_key=f"replay:{revision_id}:{key}",
                job_type="engine.run_prediction",
                payload={
                    "match_id": str(match_id),
                    "schedule_revision_id": str(revision_id),
                    "scheduled_start_at": scheduled_start.isoformat(),
                    "actual_start_at": None,
                    "cutoff_at": cutoff.isoformat(),
                    "variant_key": key,
                    "prediction_kind": kind.value,
                    "stage": "pregame",
                },
                due_at=cutoff,
                deadline_at=cutoff + timedelta(seconds=300),
                match_id=match_id,
                schedule_revision_id=revision_id,
                stage="pregame",
                variant_id=variant_id,
            )
    jobs = [_lease_next(engine, now=cutoff) for _ in variant_jobs]
    orchestrator = PredictionOrchestrator(
        engine,
        snapshot_factory=factory,
        statistical=statistical,
        providers=providers,
        budget=BudgetLedger(Decimal("10")),
        now=clock,
    )
    outcomes = [orchestrator.execute(job, now=cutoff) for job in jobs]
    repeated = orchestrator.execute(jobs[1], now=cutoff)
    restart_factory = SnapshotFactory()
    restarted = PredictionOrchestrator(
        engine,
        snapshot_factory=restart_factory,
        statistical=statistical,
        providers=providers,
        budget=BudgetLedger(Decimal("10")),
        now=clock,
    )
    after_restart = restarted.execute(jobs[2], now=cutoff)

    with engine.begin() as connection:
        assert {outcome.status for outcome in outcomes} == {ExecutionStatus.SUCCEEDED}
        assert repeated.status is ExecutionStatus.ALREADY_SUCCEEDED
        assert after_restart.status is ExecutionStatus.ALREADY_SUCCEEDED
        assert factory.calls == 1
        assert restart_factory.calls == 0
        assert statistical_runner.calls == 1
        assert all(len(transport.invocations) == 1 for transport in transports.values())
        snapshots = connection.execute(
            text(
                """
                SELECT id, sha256
                FROM engine.feature_snapshots
                WHERE schedule_revision_id = :revision_id
                """
            ),
            {"revision_id": revision_id},
        ).all()
        predictions = connection.execute(
            text(
                """
                SELECT snapshot_id, count(*), count(DISTINCT sha256)
                FROM engine.predictions
                WHERE schedule_revision_id = :revision_id
                GROUP BY snapshot_id
                """
            ),
            {"revision_id": revision_id},
        ).all()
        assert len(snapshots) == 1
        assert [tuple(row) for row in predictions] == [(snapshots[0][0], 4, 4)]


def test_completion_at_match_start_is_diagnostic_only(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    scheduled_start = NOW + timedelta(hours=1)
    cutoff = scheduled_start - timedelta(minutes=60)
    match_id, revision_id = _insert_match(
        postgres_role_urls,
        scheduled_start_at=scheduled_start,
    )
    engine = create_engine(postgres_role_urls.engine)
    clock = FakeClock(cutoff)
    runner = StatisticalRunner("statistical-late-v1", clock)
    runner.advance_on_call = timedelta(hours=1)
    factory = SnapshotFactory()

    with engine.begin() as connection:
        variant_id = _insert_variant(
            connection,
            provider="statistical",
            key="statistical-late-v1",
            model="synthetic-statistical-v1",
            prompt_hash="f" * 64,
        )
        JobRepository(connection).enqueue(
            job_key=f"late:{revision_id}",
            job_type="engine.run_prediction",
            payload={
                "match_id": str(match_id),
                "schedule_revision_id": str(revision_id),
                "scheduled_start_at": scheduled_start.isoformat(),
                "actual_start_at": None,
                "cutoff_at": cutoff.isoformat(),
                "variant_key": "statistical-late-v1",
                "prediction_kind": "statistical",
                "stage": "pregame",
            },
            due_at=cutoff,
            deadline_at=cutoff + timedelta(seconds=300),
            match_id=match_id,
            schedule_revision_id=revision_id,
            stage="pregame",
            variant_id=variant_id,
        )
    job = _lease_next(engine, now=cutoff)
    orchestrator = PredictionOrchestrator(
        engine,
        snapshot_factory=factory,
        statistical={"statistical-late-v1": StatisticalBinding(variant_id, runner)},
        providers={},
        budget=BudgetLedger(Decimal("0")),
        now=clock,
    )
    outcome = orchestrator.execute(job, now=cutoff)
    with engine.begin() as connection:
        prediction_count = connection.execute(
            text(
                "SELECT count(*) FROM engine.predictions WHERE schedule_revision_id = :revision_id"
            ),
            {"revision_id": revision_id},
        ).scalar_one()
        attempt = connection.execute(
            text(
                """
                SELECT status, error_code
                FROM engine.prediction_attempts
                WHERE job_id = :job_id
                """
            ),
            {"job_id": job["id"]},
        ).one()

        assert outcome.status is ExecutionStatus.LATE_REJECTED
        assert prediction_count == 0
        assert tuple(attempt) == ("late_rejected", "late_response")


def test_timed_out_provider_late_thread_cannot_inject_a_prediction(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    scheduled_start = NOW + timedelta(hours=1)
    cutoff = scheduled_start - timedelta(minutes=60)
    match_id, revision_id = _insert_match(
        postgres_role_urls,
        scheduled_start_at=scheduled_start,
    )
    engine = create_engine(postgres_role_urls.engine)
    clock = FakeClock(cutoff)
    variant = _variant(ProviderName.OPENAI)

    def delayed(_: object) -> bytes:
        time.sleep(0.05)
        return _provider_response(ProviderName.OPENAI, variant)

    transport = FakeProviderTransport(handler=delayed)
    timing = SchedulerTiming(request_timeout=timedelta(milliseconds=10))
    with engine.begin() as connection:
        variant_id = _insert_variant(
            connection,
            provider="openai",
            key=variant.variant_id,
            model=variant.requested_model_id,
            prompt_hash=PROMPT_TEMPLATE_HASH,
        )
        JobRepository(connection).enqueue(
            job_key=f"timeout:{revision_id}",
            job_type="engine.run_prediction",
            payload={
                "match_id": str(match_id),
                "schedule_revision_id": str(revision_id),
                "scheduled_start_at": scheduled_start.isoformat(),
                "actual_start_at": None,
                "cutoff_at": cutoff.isoformat(),
                "variant_key": variant.variant_id,
                "prediction_kind": "provider",
                "stage": "pregame",
            },
            due_at=cutoff,
            deadline_at=cutoff + timedelta(seconds=300),
            match_id=match_id,
            schedule_revision_id=revision_id,
            stage="pregame",
            variant_id=variant_id,
        )
    job = _lease_next(engine, now=cutoff)
    factory = SnapshotFactory()
    orchestrator = PredictionOrchestrator(
        engine,
        snapshot_factory=factory,
        statistical={},
        providers={
            variant.variant_id: ProviderBinding(
                variant_id,
                OpenAIPredictionProvider(variant, transport, PREDICTION_SCHEMA),
            )
        },
        budget=BudgetLedger(Decimal("10")),
        durable_budget_limits={
            variant.variant_id: ProviderBudgetLimits(
                daily_amount=Decimal("1"),
                monthly_amount=Decimal("1"),
                daily_calls=3,
                monthly_calls=3,
            )
        },
        timing=timing,
        now=clock,
    )
    outcome = orchestrator.execute(job, now=cutoff)
    time.sleep(0.08)
    with engine.begin() as connection:
        prediction_count = connection.execute(
            text(
                "SELECT count(*) FROM engine.predictions WHERE schedule_revision_id = :revision_id"
            ),
            {"revision_id": revision_id},
        ).scalar_one()
        budget_row = connection.execute(
            text(
                """
                SELECT reserved_amount, settled_amount, conservative_charge
                FROM ops.provider_budget_reservations
                WHERE job_id = :job_id
                """
            ),
            {"job_id": job["id"]},
        ).one()
        assert outcome.status is ExecutionStatus.RETRYABLE_FAILURE
        assert outcome.error_code == "timeout"
        assert prediction_count == 0
        assert budget_row[0] == budget_row[1]
        assert budget_row[2] is True


def test_semantic_job_identity_is_unique_even_when_job_keys_differ(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    scheduled_start = NOW + timedelta(hours=3)
    match_id, revision_id = _insert_match(
        postgres_role_urls,
        scheduled_start_at=scheduled_start,
    )
    engine = create_engine(postgres_role_urls.engine)
    with engine.begin() as connection:
        variant_id = _insert_variant(
            connection,
            provider="statistical",
            key=f"semantic-{uuid4()}",
            model="semantic-v1",
            prompt_hash="9" * 64,
        )
        queue = JobRepository(connection)
        common = {
            "job_type": "engine.run_prediction",
            "payload": {},
            "due_at": NOW,
            "deadline_at": NOW + timedelta(minutes=5),
            "match_id": match_id,
            "schedule_revision_id": revision_id,
            "stage": "pregame",
            "variant_id": variant_id,
        }
        first = queue.enqueue(job_key=f"semantic-a:{uuid4()}", **common)
        second = queue.enqueue(job_key=f"semantic-b:{uuid4()}", **common)
        assert first["id"] == second["id"]
        assert (
            connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM ops.jobs
                    WHERE schedule_revision_id = :revision_id
                      AND stage = 'pregame'
                      AND variant_id = :variant_id
                    """
                ),
                {"revision_id": revision_id, "variant_id": variant_id},
            ).scalar_one()
            == 1
        )
        queue.cancel_schedule_revision(
            schedule_revision_id=revision_id,
            now=NOW,
            error_code="test_cleanup",
        )


def test_running_job_requires_lease_started_at_and_leasing_populates_it(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = create_engine(postgres_role_urls.engine)
    with engine.begin() as connection:
        row = JobRepository(connection).enqueue(
            job_key=f"lease-started:{uuid4()}",
            job_type="synthetic.lease_started",
            payload={},
            due_at=NOW,
            deadline_at=NOW + timedelta(minutes=5),
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ops.jobs
                SET state = 'running', lease_owner = 'invalid',
                    lease_until = :lease_until
                WHERE id = :job_id
                """
            ),
            {"job_id": row["id"], "lease_until": NOW + timedelta(minutes=1)},
        )
    leased = _lease_next(engine, now=NOW, owner="lease-constraint-worker")
    assert leased["id"] == row["id"]
    assert leased["lease_started_at"] == NOW
    with engine.begin() as connection:
        assert JobRepository(connection).finish(
            job_id=leased["id"],
            lease_owner="lease-constraint-worker",
            succeeded=True,
            error_code=None,
            completed_at=NOW + timedelta(seconds=1),
        )


def test_durable_provider_budget_is_atomic_across_workers_and_restart(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = create_engine(postgres_role_urls.engine)
    with engine.begin() as connection:
        jobs = [
            JobRepository(connection).enqueue(
                job_key=f"budget:{uuid4()}",
                job_type="synthetic.budget",
                payload={},
                due_at=NOW + timedelta(days=30),
                deadline_at=NOW + timedelta(days=31),
            )
            for _ in range(2)
        ]
    limits = ProviderBudgetLimits(
        daily_amount=Decimal("1"),
        monthly_amount=Decimal("1"),
        daily_calls=1,
        monthly_calls=1,
    )
    provider_key = f"synthetic-budget-{uuid4()}"
    ledgers = [
        PostgresBudgetLedger(
            engine,
            reservation_key=f"budget-reservation:{job['id']}",
            provider=provider_key,
            job_id=job["id"],
            limits=limits,
            reserved_at=NOW,
        )
        for job in jobs
    ]
    barrier = threading.Barrier(2)

    def reserve(ledger: PostgresBudgetLedger) -> bool:
        barrier.wait(timeout=2)
        return ledger.reserve(Decimal("1"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, ledgers))
    assert sorted(results) == [False, True]
    winner = results.index(True)
    ledgers[winner].settle(Decimal("1"), Decimal("1"))
    restarted = PostgresBudgetLedger(
        engine,
        reservation_key=f"budget-reservation:{jobs[winner]['id']}",
        provider=provider_key,
        job_id=jobs[winner]["id"],
        limits=limits,
        reserved_at=NOW,
    )
    assert restarted.reserve(Decimal("1"))
    restarted.settle(Decimal("1"), Decimal("1"))
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT count(*), sum(settled_amount), bool_and(conservative_charge)
                FROM ops.provider_budget_reservations
                WHERE provider = :provider AND budget_day = :budget_day
                """
            ),
            {"provider": provider_key, "budget_day": NOW.date()},
        ).one()
        assert tuple(row) == (1, Decimal("1.00000000"), True)


def test_parallel_dispatch_uses_separate_connections_before_start_tolerance(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    scheduled_start = NOW + timedelta(hours=1)
    cutoff = scheduled_start - timedelta(minutes=60)
    match_id, revision_id = _insert_match(
        postgres_role_urls,
        scheduled_start_at=scheduled_start,
    )
    engine = create_engine(postgres_role_urls.engine, pool_size=4)
    clock = FakeClock(cutoff)
    variants = {
        provider_name: _variant(provider_name)
        for provider_name in (ProviderName.OPENAI, ProviderName.ANTHROPIC)
    }
    barrier = threading.Barrier(2)

    def synchronized_response(invocation: Any) -> bytes:
        barrier.wait(timeout=2)
        time.sleep(0.05)
        return _provider_response(invocation.provider, variants[invocation.provider])

    transport = FakeProviderTransport(handler=synchronized_response)
    provider_bindings: dict[str, ProviderBinding] = {}
    with engine.begin() as connection:
        queue = JobRepository(connection)
        for provider_name, variant in variants.items():
            variant_id = _insert_variant(
                connection,
                provider=provider_name.value,
                key=variant.variant_id,
                model=variant.requested_model_id,
                prompt_hash=PROMPT_TEMPLATE_HASH,
            )
            provider_bindings[variant.variant_id] = ProviderBinding(
                variant_id,
                _provider_adapter(provider_name, variant, transport),
            )
            queue.enqueue(
                job_key=f"parallel:{revision_id}:{variant.variant_id}",
                job_type="engine.run_prediction",
                payload={
                    "match_id": str(match_id),
                    "schedule_revision_id": str(revision_id),
                    "scheduled_start_at": scheduled_start.isoformat(),
                    "actual_start_at": None,
                    "cutoff_at": cutoff.isoformat(),
                    "variant_key": variant.variant_id,
                    "prediction_kind": "provider",
                    "stage": "pregame",
                },
                due_at=cutoff,
                deadline_at=cutoff + timedelta(minutes=5),
                match_id=match_id,
                schedule_revision_id=revision_id,
                stage="pregame",
                variant_id=variant_id,
            )
    factory = SnapshotFactory()
    orchestrator = PredictionOrchestrator(
        engine,
        snapshot_factory=factory,
        statistical={},
        providers=provider_bindings,
        budget=BudgetLedger(Decimal("10")),
        now=clock,
    )
    dispatcher = PredictionJobDispatcher(
        engine,
        scheduler_handlers(orchestrator),
        lease_owner="parallel-worker",
        max_concurrency=2,
        now=clock,
    )
    assert dispatcher.run_once(now=cutoff) == 2
    with engine.begin() as connection:
        assert (
            connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM engine.predictions
                    WHERE match_id = :match_id
                    """
                ),
                {"match_id": match_id},
            ).scalar_one()
            == 2
        )
    assert len(transport.invocations) == 2
    assert factory.calls == 1


@pytest.mark.parametrize(
    ("status", "actual_start_offset"),
    (("cancelled", None), ("live", timedelta(seconds=1))),
)
def test_in_flight_schedule_change_revokes_lease_and_discards_provider_result(
    postgres_role_urls: PostgresRoleUrls,
    status: str,
    actual_start_offset: timedelta | None,
) -> None:
    scheduled_start = NOW + timedelta(hours=1)
    cutoff = scheduled_start - timedelta(minutes=60)
    match_id, revision_id = _insert_match(
        postgres_role_urls,
        scheduled_start_at=scheduled_start,
    )
    engine = create_engine(postgres_role_urls.engine)
    clock = FakeClock(cutoff)
    variant = _variant(ProviderName.OPENAI)
    started = threading.Event()
    release = threading.Event()

    def delayed(_: object) -> bytes:
        started.set()
        assert release.wait(timeout=2)
        return _provider_response(ProviderName.OPENAI, variant)

    transport = FakeProviderTransport(handler=delayed)
    with engine.begin() as connection:
        variant_id = _insert_variant(
            connection,
            provider="openai",
            key=variant.variant_id,
            model=variant.requested_model_id,
            prompt_hash=PROMPT_TEMPLATE_HASH,
        )
        JobRepository(connection).enqueue(
            job_key=f"in-flight-cancel:{revision_id}",
            job_type="engine.run_prediction",
            payload={
                "match_id": str(match_id),
                "schedule_revision_id": str(revision_id),
                "scheduled_start_at": scheduled_start.isoformat(),
                "actual_start_at": None,
                "cutoff_at": cutoff.isoformat(),
                "variant_key": variant.variant_id,
                "prediction_kind": "provider",
                "stage": "pregame",
            },
            due_at=cutoff,
            deadline_at=cutoff + timedelta(minutes=5),
            match_id=match_id,
            schedule_revision_id=revision_id,
            stage="pregame",
            variant_id=variant_id,
        )
    job = _lease_next(engine, now=cutoff, owner="cancel-worker")
    orchestrator = PredictionOrchestrator(
        engine,
        snapshot_factory=SnapshotFactory(),
        statistical={},
        providers={
            variant.variant_id: ProviderBinding(
                variant_id,
                OpenAIPredictionProvider(variant, transport, PREDICTION_SCHEMA),
            )
        },
        budget=BudgetLedger(Decimal("10")),
        now=clock,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(orchestrator.execute, job, now=cutoff)
        assert started.wait(timeout=2)
        observed_at = cutoff + timedelta(seconds=1)
        cancelled_revision_id = _insert_match_revision(
            postgres_role_urls,
            previous_revision_id=revision_id,
            scheduled_start_at=scheduled_start,
            status=status,
            observed_at=observed_at,
            actual_start_at=(
                cutoff + actual_start_offset if actual_start_offset is not None else None
            ),
        )
        previous = _schedule(match_id, revision_id)
        previous = replace(previous, scheduled_start_at=scheduled_start)
        current = replace(
            previous,
            id=cancelled_revision_id,
            revision=2,
            status=status,
            observed_at=observed_at,
            actual_start_at=(
                cutoff + actual_start_offset if actual_start_offset is not None else None
            ),
        )
        with engine.begin() as connection:
            ScheduleLifecycleCoordinator(connection).reconcile(current, previous)
        release.set()
        outcome = future.result(timeout=2)
    assert outcome.status is ExecutionStatus.LATE_REJECTED
    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM engine.predictions WHERE match_id = :match_id"),
                {"match_id": match_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT state FROM ops.jobs WHERE id = :job_id"),
                {"job_id": job["id"]},
            ).scalar_one()
            == "cancelled"
        )
        assert (
            connection.execute(
                text(
                    """
                SELECT status
                FROM engine.prediction_attempts
                WHERE job_id = :job_id
                ORDER BY created_at DESC
                LIMIT 1
                """
                ),
                {"job_id": job["id"]},
            ).scalar_one()
            == "late_rejected"
        )


def test_worker_entrypoint_ticks_schedules_and_executes_synthetic_db_job(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    scheduled_start = NOW + timedelta(hours=1)
    cutoff = scheduled_start - timedelta(minutes=60)
    match_id, revision_id = _insert_match(
        postgres_role_urls,
        scheduled_start_at=scheduled_start,
    )
    engine = create_engine(postgres_role_urls.engine)
    clock = FakeClock(cutoff)
    factory = SnapshotFactory()
    runner = StatisticalRunner("entrypoint-statistical-v1", clock)
    with engine.begin() as connection:
        statistical_id = _insert_variant(
            connection,
            provider="statistical",
            key="entrypoint-statistical-v1",
            model="entrypoint-statistical-v1",
            prompt_hash="8" * 64,
        )
        provider_bindings: dict[str, ProviderBinding] = {}
        for provider_name in ProviderName:
            variant = _variant(provider_name)
            variant_id = _insert_variant(
                connection,
                provider=provider_name.value,
                key=variant.variant_id,
                model=variant.requested_model_id,
                prompt_hash=PROMPT_TEMPLATE_HASH,
            )
            provider_bindings[variant.variant_id] = ProviderBinding(
                variant_id,
                _provider_adapter(
                    provider_name,
                    variant,
                    FakeProviderTransport(response_body=_provider_response(provider_name, variant)),
                ),
            )
        JobRepository(connection).enqueue(
            job_key=f"entrypoint:{revision_id}:statistical",
            job_type="engine.run_prediction",
            payload={
                "match_id": str(match_id),
                "schedule_revision_id": str(revision_id),
                "scheduled_start_at": scheduled_start.isoformat(),
                "actual_start_at": None,
                "cutoff_at": cutoff.isoformat(),
                "variant_key": "entrypoint-statistical-v1",
                "prediction_kind": "statistical",
                "stage": "pregame",
            },
            due_at=cutoff,
            deadline_at=cutoff + timedelta(minutes=5),
            match_id=match_id,
            schedule_revision_id=revision_id,
            stage="pregame",
            variant_id=statistical_id,
        )

    class PreCutoffSyncHandler:
        def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
            raise AssertionError("entrypoint fixture must lease the pre-enqueued prediction first")

    components = WorkerRuntimeComponents(
        snapshot_factory=factory,
        statistical={"entrypoint-statistical-v1": StatisticalBinding(statistical_id, runner)},
        providers=provider_bindings,
        sync_handlers={"mirror.pre_cutoff_sync": PreCutoffSyncHandler()},
        budget=BudgetLedger(Decimal("10")),
        durable_budget_limits={},
        max_concurrency=1,
    )
    assert (
        main(
            ["--once"],
            settings=Settings(database_url=postgres_role_urls.engine),
            environ={"VLYTICS_DATABASE_URL": postgres_role_urls.engine},
            runtime_components=components,
            now=clock,
        )
        == 0
    )
    with engine.begin() as connection:
        assert (
            connection.execute(
                text(
                    """
                SELECT count(*)
                FROM engine.predictions
                WHERE match_id = :match_id
                  AND schedule_revision_id = :revision_id
                """
                ),
                {"match_id": match_id, "revision_id": revision_id},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text(
                    """
                SELECT state
                FROM ops.jobs
                WHERE schedule_revision_id = :revision_id
                  AND variant_id = :variant_id
                """
                ),
                {"revision_id": revision_id, "variant_id": statistical_id},
            ).scalar_one()
            == "succeeded"
        )
    observed_at = cutoff + timedelta(seconds=2)
    replacement_start = scheduled_start + timedelta(hours=1)
    replacement_id = _insert_match_revision(
        postgres_role_urls,
        previous_revision_id=revision_id,
        scheduled_start_at=replacement_start,
        status="scheduled",
        observed_at=observed_at,
    )
    previous = replace(_schedule(match_id, revision_id), scheduled_start_at=scheduled_start)
    current = replace(
        previous,
        id=replacement_id,
        revision=2,
        scheduled_start_at=replacement_start,
        observed_at=observed_at,
    )
    with engine.begin() as connection:
        ScheduleLifecycleCoordinator(connection).reconcile(current, previous)
        assert (
            connection.execute(
                text(
                    """
                SELECT current_status
                FROM engine.prediction_status_projection AS projection
                JOIN engine.predictions AS prediction
                  ON prediction.id = projection.prediction_id
                WHERE prediction.schedule_revision_id = :revision_id
                """
                ),
                {"revision_id": revision_id},
            ).scalar_one()
            == "voided"
        )
        assert (
            connection.execute(
                text(
                    """
                SELECT count(*)
                FROM ops.jobs
                WHERE schedule_revision_id = :revision_id
                  AND state IN ('queued', 'running', 'retry_wait')
                """
                ),
                {"revision_id": revision_id},
            ).scalar_one()
            == 0
        )


def test_worker_entrypoint_builds_default_production_runtime(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = create_engine(postgres_role_urls.engine)

    assert (
        main(
            ["--once"],
            settings=Settings(database_url=postgres_role_urls.engine),
            environ={"VLYTICS_DATABASE_URL": postgres_role_urls.engine},
            now=lambda: NOW,
        )
        == 0
    )

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                    SELECT provider, requested_model, hyperparameters->>'variant_key'
                    FROM engine.model_variants
                    WHERE hyperparameters->>'variant_key' = 'statistical-joint-v1'
                    """
            )
        ).one()
        assert row == (
            "statistical",
            "feature-form-p5-joint-v1",
            "statistical-joint-v1",
        )
        assert (
            connection.execute(
                text("SELECT to_regclass('engine.joint_score_distributions')")
            ).scalar_one()
            == "engine.joint_score_distributions"
        )
