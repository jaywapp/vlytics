from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from vlytics.engine.evaluation import (
    COHORT_POLICY_VERSION,
    EVALUATION_JOB_TYPE,
    EVALUATOR_VERSION,
    CohortKey,
    EvaluationWorkItem,
    PostgresEvaluationWorkReader,
    PredictionEvaluationInput,
    ResultEvaluationService,
    ResultEvaluationWork,
    ResultRevision,
    evaluation_job_key,
    result_revision_id_from_job,
)
from vlytics.engine.evaluation.runtime import EvaluationJobHandler
from vlytics.engine.market import EvaluationEligibility, MarketEvaluation
from vlytics.ops.scheduler import PredictionJobDispatcher, TerminalJobError

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 20, 3, tzinfo=UTC)
MATCH_ID = "11111111-1111-1111-1111-111111111111"
PREDICTION_ID = "22222222-2222-2222-2222-222222222222"
SCHEDULE_ID = "33333333-3333-3333-3333-333333333333"
SNAPSHOT_ID = "44444444-4444-4444-4444-444444444444"
RESULT_ID = UUID("55555555-5555-5555-5555-555555555555")
CORRECTED_RESULT_ID = UUID("66666666-6666-6666-6666-666666666666")
TAMPERED_PREDICTION_ID = UUID("88888888-8888-8888-8888-888888888888")


@dataclass(frozen=True)
class _Stored:
    created: bool


class _QueryResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def mappings(self) -> _QueryResult:
        return self

    def one_or_none(self) -> object:
        return self.value

    def all(self) -> object:
        return self.value


class _QueryConnection:
    def __init__(self, value: object) -> None:
        self.value = value

    def execute(self, *_args: object, **_kwargs: object) -> _QueryResult:
        return _QueryResult(self.value)


class _Reader:
    def __init__(self, work: dict[UUID, ResultEvaluationWork]) -> None:
        self.work = work

    def load(
        self,
        result_revision_id: UUID,
        *,
        trigger_prediction_id: UUID | None = None,
    ) -> ResultEvaluationWork:
        del trigger_prediction_id
        return self.work[result_revision_id]


class _ScopedQueryConnection:
    def __init__(self) -> None:
        self.prediction_queries: list[tuple[str, dict[str, object]]] = []

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _QueryResult:
        sql = str(statement)
        values = parameters or {}
        if "FROM mirror.result_revisions rr" in sql:
            return _QueryResult(
                {
                    "id": RESULT_ID,
                    "match_id": UUID(MATCH_ID),
                    "revision": 1,
                    "finality": "final",
                    "home_sets": 3,
                    "away_sets": 1,
                    "home_points": 75,
                    "away_points": 63,
                    "division": "men",
                    "competition": "regular-vleague",
                    "stage": "regular",
                }
            )
        if "FROM engine.predictions p" in sql and "match_id" in values:
            self.prediction_queries.append((sql, values))
            rows = [
                _prediction_row(UUID(PREDICTION_ID)),
                _prediction_row(TAMPERED_PREDICTION_ID),
            ]
            if "FROM market.market_evaluations market_evaluation" in sql:
                rows = [row for row in rows if row["id"] == UUID(PREDICTION_ID)]
            if "p.id = CAST(:trigger_prediction_id AS uuid)" in sql:
                trigger_prediction_id = values.get("trigger_prediction_id")
                if trigger_prediction_id is not None:
                    rows = [row for row in rows if row["id"] == trigger_prediction_id]
            return _QueryResult(rows)
        if "FROM market.market_evaluations me" in sql:
            prediction_id = values["prediction_id"]
            if prediction_id == UUID(PREDICTION_ID):
                return _QueryResult(
                    {
                        "market_snapshot_id": None,
                        "evaluator_version": "market-evaluator-v1",
                        "eligibility": EvaluationEligibility.MISSING.value,
                        "reason": "no_market_evaluation_for_prediction",
                        "derived_probabilities": {},
                    }
                )
            return _QueryResult(None)
        raise AssertionError(f"unexpected SQL: {sql}")


class _Transaction:
    def __init__(self, connection: _ScopedQueryConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _ScopedQueryConnection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _ScopedQueryConnection) -> None:
        self.connection = connection

    def begin(self) -> _Transaction:
        return _Transaction(self.connection)


class _RecordingJobRepository:
    quarantined: list[tuple[UUID, str]] = []
    retries: list[UUID] = []

    def __init__(self, _connection: object) -> None:
        pass

    def quarantine(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        error_code: str,
        now: datetime,
    ) -> bool:
        del lease_owner, now
        self.quarantined.append((job_id, error_code))
        return True

    def schedule_retry(self, *, job_id: UUID, **_kwargs: object) -> bool:
        self.retries.append(job_id)
        return True


class _Writer:
    def __init__(self) -> None:
        self.keys: set[tuple[str, str, str, str]] = set()
        self.evaluations: list[Any] = []

    def add(self, evaluation: Any) -> _Stored:
        key = (
            evaluation.prediction.prediction_id,
            evaluation.result.result_revision_id,
            evaluation.evaluator_version,
            evaluation.cohort_policy_version,
        )
        created = key not in self.keys
        self.keys.add(key)
        self.evaluations.append(evaluation)
        return _Stored(created)


def _prediction_row(prediction_id: UUID) -> dict[str, object]:
    return {
        "id": prediction_id,
        "match_id": UUID(MATCH_ID),
        "schedule_revision_id": UUID(SCHEDULE_ID),
        "snapshot_id": UUID(SNAPSHOT_ID),
        "input_cutoff_at": NOW,
        "resolved_model_id": "resolved-model-v1",
        "output_json": {
            "home_win_probability": 0.7,
            "set_score_probabilities": _prediction().set_score_probabilities,
        },
        "provider": "openai",
        "pinned_model_version": "pinned-model-v1",
        "prompt_version": "prompt-v1",
        "feature_version": "feature-v1",
        "availability_policy": "live_prospective",
    }


def _prediction(finality: str = "final") -> PredictionEvaluationInput:
    return PredictionEvaluationInput(
        prediction_id=PREDICTION_ID,
        match_id=MATCH_ID,
        schedule_revision_id=SCHEDULE_ID,
        snapshot_id=SNAPSHOT_ID,
        input_cutoff_at=NOW,
        cohort=CohortKey(
            division="men",
            competition="regular-vleague",
            stage="regular",
            provider="openai",
            model_version="pinned-model-v1",
            prompt_version="prompt-v1",
            feature_version="feature-v1",
            availability_policy="live_prospective",
            timing_eligibility="on_time",
            result_finality=finality,
        ),
        home_win_probability=0.7,
        set_score_probabilities={
            "3:0": 0.2,
            "3:1": 0.2,
            "3:2": 0.3,
            "2:3": 0.1,
            "1:3": 0.1,
            "0:3": 0.1,
        },
    )


def _result(
    result_id: UUID, revision: int, finality: str, score: tuple[int, int]
) -> ResultRevision:
    return ResultRevision(
        result_revision_id=str(result_id),
        match_id=MATCH_ID,
        revision=revision,
        finality=finality,
        home_sets=score[0],
        away_sets=score[1],
        home_points=75,
        away_points=63,
    )


def _work(result: ResultRevision) -> ResultEvaluationWork:
    market = MarketEvaluation(
        prediction_id=PREDICTION_ID,
        match_id=MATCH_ID,
        snapshot_id=None,
        evaluator_version="market-evaluator-v1",
        eligibility=EvaluationEligibility.MISSING,
        reason="no_market_evaluation_for_prediction",
        lines=(),
    )
    return ResultEvaluationWork(
        result=result,
        predictions=(EvaluationWorkItem(_prediction(result.finality), market, None),),
    )


def test_runtime_evaluation_is_idempotent_and_corrections_append() -> None:
    original = _result(RESULT_ID, 1, "final", (3, 1))
    corrected = _result(CORRECTED_RESULT_ID, 2, "corrected", (1, 3))
    writer = _Writer()
    service = ResultEvaluationService(
        _Reader({RESULT_ID: _work(original), CORRECTED_RESULT_ID: _work(corrected)}),
        writer,
    )

    first = service.evaluate_result(RESULT_ID)
    retry = service.evaluate_result(RESULT_ID)
    correction = service.evaluate_result(CORRECTED_RESULT_ID)

    assert (first.evaluations_created, first.evaluations_existing) == (1, 0)
    assert (retry.evaluations_created, retry.evaluations_existing) == (0, 1)
    assert (correction.evaluations_created, correction.evaluations_existing) == (1, 0)
    assert len(writer.keys) == 2
    assert writer.evaluations[0].brier != writer.evaluations[-1].brier


def test_runtime_preserves_exact_provenance_and_missing_market() -> None:
    result = _result(RESULT_ID, 1, "provisional", (3, 2))
    writer = _Writer()
    summary = ResultEvaluationService(_Reader({RESULT_ID: _work(result)}), writer).evaluate_result(
        RESULT_ID
    )

    assert summary.published_predictions == 1
    evaluation = writer.evaluations[0]
    assert evaluation.eligible is True
    assert evaluation.market_status == "missing"
    assert evaluation.market_settlements == ()
    provenance = evaluation.metric_values()["evaluation_input"]
    assert provenance == {
        "prediction_id": PREDICTION_ID,
        "match_id": MATCH_ID,
        "schedule_revision_id": SCHEDULE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "input_cutoff_at": NOW.isoformat(),
        "result_revision_id": str(RESULT_ID),
        "result_revision": 1,
        "result_finality": "provisional",
    }


def test_manual_and_trigger_scoped_evaluation_exclude_bad_hash_peer() -> None:
    connection = _ScopedQueryConnection()
    reader = PostgresEvaluationWorkReader(connection)  # type: ignore[arg-type]

    manual_writer = _Writer()
    manual_summary = ResultEvaluationService(reader, manual_writer).evaluate_result(RESULT_ID)

    scoped_writer = _Writer()
    scoped_summary = ResultEvaluationService(reader, scoped_writer).evaluate_result(
        RESULT_ID,
        trigger_prediction_id=UUID(PREDICTION_ID),
    )

    assert manual_summary.published_predictions == 1
    assert manual_summary.evaluations_created == 1
    assert manual_writer.evaluations[0].prediction.prediction_id == PREDICTION_ID
    manual_sql, manual_parameters = connection.prediction_queries[-2]
    assert "FROM market.market_evaluations market_evaluation" in manual_sql
    assert manual_parameters["trigger_prediction_id"] is None
    assert scoped_summary.published_predictions == 1
    assert scoped_summary.evaluations_created == 1
    assert scoped_writer.evaluations[0].prediction.prediction_id == PREDICTION_ID
    _, scoped_parameters = connection.prediction_queries[-1]
    assert scoped_parameters["trigger_prediction_id"] == UUID(PREDICTION_ID)


def test_bad_hash_trigger_is_quarantined_without_retry_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _ScopedQueryConnection()
    engine = _Engine(connection)
    handler = EvaluationJobHandler(engine)  # type: ignore[arg-type]
    job_id = UUID("99999999-9999-9999-9999-999999999999")
    job = {
        "id": job_id,
        "job_type": EVALUATION_JOB_TYPE,
        "attempt_no": 1,
        "payload": {
            "result_revision_id": str(RESULT_ID),
            "trigger_prediction_id": str(TAMPERED_PREDICTION_ID),
            "evaluator_version": EVALUATOR_VERSION,
            "cohort_policy_version": COHORT_POLICY_VERSION,
        },
    }
    _RecordingJobRepository.quarantined = []
    _RecordingJobRepository.retries = []
    monkeypatch.setattr(
        "vlytics.ops.scheduler.JobRepository",
        _RecordingJobRepository,
    )
    dispatcher = PredictionJobDispatcher(
        engine,  # type: ignore[arg-type]
        {EVALUATION_JOB_TYPE: handler},
        lease_owner="evaluation-test-worker",
        now=lambda: NOW,
    )

    dispatcher._execute(job, NOW)

    assert _RecordingJobRepository.quarantined == [(job_id, "ineligible_evaluation_input")]
    assert _RecordingJobRepository.retries == []


def test_evaluation_job_contract_is_versioned_and_fail_closed() -> None:
    job = {
        "job_type": EVALUATION_JOB_TYPE,
        "payload": {
            "result_revision_id": str(RESULT_ID),
            "evaluator_version": EVALUATOR_VERSION,
            "cohort_policy_version": COHORT_POLICY_VERSION,
        },
    }
    assert result_revision_id_from_job(job) == RESULT_ID
    assert evaluation_job_key(RESULT_ID) == (
        f"evaluation:{RESULT_ID}:{EVALUATOR_VERSION}:{COHORT_POLICY_VERSION}"
    )
    trigger_prediction_id = UUID(PREDICTION_ID)
    assert evaluation_job_key(RESULT_ID, trigger_prediction_id) == (
        f"evaluation:{RESULT_ID}:{trigger_prediction_id}:"
        f"{EVALUATOR_VERSION}:{COHORT_POLICY_VERSION}"
    )

    invalid = {**job, "payload": {**job["payload"], "evaluator_version": "unknown"}}
    with pytest.raises(ValueError, match="evaluator_version"):
        result_revision_id_from_job(invalid)


def test_old_evaluation_job_contract_is_quarantined_before_database_access() -> None:
    job = {
        "job_type": EVALUATION_JOB_TYPE,
        "payload": {
            "result_revision_id": str(RESULT_ID),
            "evaluator_version": EVALUATOR_VERSION,
            "cohort_policy_version": "performance-cohort-v1",
        },
    }
    handler = EvaluationJobHandler(None)  # type: ignore[arg-type]

    with pytest.raises(TerminalJobError, match="unsupported_evaluation_job_contract"):
        handler.handle(job, now=NOW)


def test_postgres_runtime_sql_selects_only_published_predictions_and_pending_results() -> None:
    source = (ROOT / "src" / "vlytics" / "engine" / "evaluation" / "runtime.py").read_text(
        encoding="utf-8"
    )

    assert "status.current_status = 'published'" in source
    assert "WHERE p.match_id = :match_id" in source
    assert "WHERE NOT EXISTS" in source
    assert "result_revision_id" in source
    assert "with self._engine.begin() as connection" in source


@pytest.mark.parametrize(
    ("eligibility", "snapshot_id"),
    [
        (EvaluationEligibility.MISSING, None),
        (EvaluationEligibility.STALE, None),
        (EvaluationEligibility.LATE, None),
        (EvaluationEligibility.UNSUPPORTED, UUID("77777777-7777-7777-7777-777777777777")),
    ],
)
def test_postgres_work_reader_preserves_market_eligibility_and_nullable_snapshot(
    eligibility: EvaluationEligibility,
    snapshot_id: UUID | None,
) -> None:
    row = {
        "market_snapshot_id": snapshot_id,
        "evaluator_version": "market-evaluator-v1",
        "eligibility": eligibility.value,
        "reason": f"{eligibility.value}_reason",
        "derived_probabilities": {},
    }
    reader = PostgresEvaluationWorkReader(_QueryConnection(row))  # type: ignore[arg-type]

    evaluation, snapshot = reader._market_context(_prediction())

    assert evaluation.eligibility is eligibility
    assert evaluation.snapshot_id == (str(snapshot_id) if snapshot_id is not None else None)
    assert snapshot is None
