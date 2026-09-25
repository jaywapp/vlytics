"""Durable production workflow for evaluating immutable result revisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import Connection, Engine, RowMapping, text

from vlytics.engine.evaluation.evaluator import ResultEvaluator
from vlytics.engine.evaluation.models import (
    COHORT_POLICY_VERSION,
    EVALUATOR_VERSION,
    CohortKey,
    PredictionEvaluationInput,
    ResultRevision,
)
from vlytics.engine.evaluation.repository import EvaluationStore
from vlytics.engine.market import (
    EvaluationEligibility,
    LineProbabilities,
    MarketEvaluation,
    MarketLine,
    MarketLineEvaluation,
    MarketPeriod,
    MarketSelection,
    MarketSnapshot,
    MarketType,
    MarketUnit,
)
from vlytics.ops.repositories import JobRepository

EVALUATION_JOB_TYPE = "engine.evaluate_result"
MARKET_EVALUATOR_VERSION = "market-evaluator-v1"


@dataclass(frozen=True)
class EvaluationWorkItem:
    """One published prediction and its optional exact Market comparison."""

    prediction: PredictionEvaluationInput
    market_evaluation: MarketEvaluation
    market_snapshot: MarketSnapshot | None


@dataclass(frozen=True)
class ResultEvaluationWork:
    """The exact result revision and all currently published eligible predictions."""

    result: ResultRevision
    predictions: tuple[EvaluationWorkItem, ...]


@dataclass(frozen=True)
class EvaluationRunSummary:
    result_revision_id: UUID
    published_predictions: int
    evaluations_created: int
    evaluations_existing: int


class EvaluationWorkReader(Protocol):
    def load(self, result_revision_id: UUID) -> ResultEvaluationWork: ...


class EvaluationWriter(Protocol):
    def add(self, evaluation: Any) -> Any: ...


class ResultEvaluationService:
    """Evaluate all published predictions against one exact immutable result."""

    def __init__(
        self,
        reader: EvaluationWorkReader,
        writer: EvaluationWriter,
        *,
        evaluator: ResultEvaluator | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._evaluator = evaluator or ResultEvaluator()

    def evaluate_result(self, result_revision_id: UUID) -> EvaluationRunSummary:
        work = self._reader.load(result_revision_id)
        created = 0
        existing = 0
        for item in work.predictions:
            evaluation = self._evaluator.evaluate(
                item.prediction,
                work.result,
                market_evaluation=item.market_evaluation,
                market_snapshot=item.market_snapshot,
            )
            stored = self._writer.add(evaluation)
            if bool(stored.created):
                created += 1
            else:
                existing += 1
        return EvaluationRunSummary(
            result_revision_id=result_revision_id,
            published_predictions=len(work.predictions),
            evaluations_created=created,
            evaluations_existing=existing,
        )


class PostgresEvaluationWorkReader:
    """Rebuild evaluation inputs from relational identities and immutable JSON outputs."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def load(self, result_revision_id: UUID) -> ResultEvaluationWork:
        result_row = (
            self._connection.execute(
                text(
                    """
                    SELECT rr.id, rr.match_id, rr.revision, rr.finality,
                           rr.home_sets, rr.away_sets, rr.home_points, rr.away_points,
                           c.division, c.source_competition_code AS competition, c.stage
                    FROM mirror.result_revisions rr
                    JOIN mirror.matches m ON m.id = rr.match_id
                    JOIN mirror.competitions c ON c.id = m.competition_id
                    WHERE rr.id = :result_revision_id
                    """
                ),
                {"result_revision_id": result_revision_id},
            )
            .mappings()
            .one_or_none()
        )
        if result_row is None:
            raise LookupError(f"result revision {result_revision_id} does not exist")
        result = _result_from_row(result_row)
        prediction_rows = (
            self._connection.execute(
                text(
                    """
                    SELECT p.id, p.match_id, p.schedule_revision_id, p.snapshot_id,
                           p.input_cutoff_at, p.resolved_model_id, p.output_json,
                           v.provider, v.pinned_model_version, v.prompt_version,
                           s.feature_version, s.availability_policy
                    FROM engine.predictions p
                    JOIN engine.prediction_status_projection status
                      ON status.prediction_id = p.id
                     AND status.current_status = 'published'
                    JOIN engine.model_variants v ON v.id = p.variant_id
                    JOIN engine.feature_snapshots s ON s.id = p.snapshot_id
                    WHERE p.match_id = :match_id
                    ORDER BY p.input_cutoff_at, p.id
                    """
                ),
                {"match_id": UUID(result.match_id)},
            )
            .mappings()
            .all()
        )
        return ResultEvaluationWork(
            result=result,
            predictions=tuple(
                self._work_item(row, result_row=result_row) for row in prediction_rows
            ),
        )

    def _work_item(
        self,
        row: RowMapping,
        *,
        result_row: RowMapping,
    ) -> EvaluationWorkItem:
        output = _object(row["output_json"], "prediction output_json")
        availability_policy = str(row["availability_policy"])
        timing_eligibility = (
            "reconstructed" if availability_policy == "historical_reconstruction" else "on_time"
        )
        prediction = PredictionEvaluationInput(
            prediction_id=str(row["id"]),
            match_id=str(row["match_id"]),
            schedule_revision_id=str(row["schedule_revision_id"]),
            snapshot_id=str(row["snapshot_id"]),
            input_cutoff_at=cast(datetime, row["input_cutoff_at"]),
            cohort=CohortKey(
                division=str(result_row["division"]),
                competition=str(result_row["competition"]),
                stage=str(result_row["stage"]),
                provider=str(row["provider"]),
                model_version=str(row["pinned_model_version"] or row["resolved_model_id"]),
                prompt_version=str(row["prompt_version"]),
                feature_version=str(row["feature_version"]),
                availability_policy=availability_policy,
                timing_eligibility=timing_eligibility,
                result_finality=str(result_row["finality"]),
            ),
            home_win_probability=_optional_probability(output.get("home_win_probability")),
            set_score_probabilities=_set_score_probabilities(output.get("set_score_probabilities")),
        )
        market_evaluation, market_snapshot = self._market_context(prediction)
        return EvaluationWorkItem(prediction, market_evaluation, market_snapshot)

    def _market_context(
        self,
        prediction: PredictionEvaluationInput,
    ) -> tuple[MarketEvaluation, MarketSnapshot | None]:
        row = (
            self._connection.execute(
                text(
                    """
                    SELECT me.market_snapshot_id, me.evaluator_version, me.eligibility,
                           me.reason, me.derived_probabilities,
                           ms.match_id, ms.source, ms.source_event_id, ms.quoted_at,
                           ms.observed_at, ms.received_at, ms.contract_version,
                           ms.markets_json, ms.sha256
                    FROM market.market_evaluations me
                    LEFT JOIN market.market_snapshots ms ON ms.id = me.market_snapshot_id
                    WHERE me.prediction_id = :prediction_id
                      AND me.evaluator_version = :market_evaluator_version
                    ORDER BY me.created_at DESC, me.id DESC
                    LIMIT 1
                    """
                ),
                {
                    "prediction_id": UUID(prediction.prediction_id),
                    "market_evaluator_version": MARKET_EVALUATOR_VERSION,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return (
                MarketEvaluation(
                    prediction_id=prediction.prediction_id,
                    match_id=prediction.match_id,
                    snapshot_id=None,
                    evaluator_version=MARKET_EVALUATOR_VERSION,
                    eligibility=EvaluationEligibility.MISSING,
                    reason="no_market_evaluation_for_prediction",
                    lines=(),
                ),
                None,
            )
        eligibility = EvaluationEligibility(str(row["eligibility"]))
        if eligibility is not EvaluationEligibility.ELIGIBLE:
            market_snapshot_id = row["market_snapshot_id"]
            return (
                MarketEvaluation(
                    prediction_id=prediction.prediction_id,
                    match_id=prediction.match_id,
                    snapshot_id=(
                        str(market_snapshot_id) if market_snapshot_id is not None else None
                    ),
                    evaluator_version=str(row["evaluator_version"]),
                    eligibility=eligibility,
                    reason=str(row["reason"]),
                    lines=(),
                ),
                None,
            )
        snapshot = _market_snapshot_from_row(row)
        derived = _object(row["derived_probabilities"], "derived_probabilities")
        return (
            MarketEvaluation(
                prediction_id=prediction.prediction_id,
                match_id=prediction.match_id,
                snapshot_id=snapshot.snapshot_id,
                evaluator_version=str(row["evaluator_version"]),
                eligibility=EvaluationEligibility.ELIGIBLE,
                reason=str(row["reason"]),
                lines=tuple(
                    _market_line_evaluation(line.line_id, derived) for line in snapshot.markets
                ),
            ),
            snapshot,
        )


class EvaluationUnitOfWork:
    """Commit one result revision's complete evaluation batch atomically."""

    def __init__(
        self,
        connection: Connection,
        *,
        evaluator: ResultEvaluator | None = None,
    ) -> None:
        self._service = ResultEvaluationService(
            PostgresEvaluationWorkReader(connection),
            EvaluationStore(connection),
            evaluator=evaluator,
        )

    def evaluate_result(self, result_revision_id: UUID) -> EvaluationRunSummary:
        return self._service.evaluate_result(result_revision_id)


class EvaluationJobPlanner:
    """Detect unqueued result revisions and create one versioned durable job each."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._jobs = JobRepository(connection)

    def enqueue_result_revision(
        self,
        result_revision_id: UUID,
        *,
        now: datetime,
    ) -> Mapping[str, Any]:
        exists = self._connection.execute(
            text("SELECT 1 FROM mirror.result_revisions WHERE id = :result_revision_id"),
            {"result_revision_id": result_revision_id},
        ).scalar_one_or_none()
        if exists is None:
            raise LookupError(f"result revision {result_revision_id} does not exist")
        return self._enqueue(result_revision_id, now=now)

    def enqueue_pending(self, *, now: datetime, limit: int = 100) -> int:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._connection.execute(
            text(
                """
                SELECT rr.id AS result_revision_id, prediction.id AS prediction_id
                FROM mirror.result_revisions rr
                JOIN engine.predictions prediction ON prediction.match_id = rr.match_id
                JOIN engine.prediction_status_projection status
                  ON status.prediction_id = prediction.id
                 AND status.current_status = 'published'
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM engine.evaluations evaluation
                    WHERE evaluation.prediction_id = prediction.id
                      AND evaluation.result_revision_id = rr.id
                      AND evaluation.evaluator_version = :evaluator_version
                      AND evaluation.cohort_policy_version = :cohort_policy_version
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM ops.jobs job
                    WHERE job.job_key = concat(
                        'evaluation:', rr.id::text, ':', prediction.id::text, ':',
                        :evaluator_version, ':', :cohort_policy_version
                    )
                )
                ORDER BY rr.observed_at, rr.id, prediction.id
                LIMIT :limit
                """
            ),
            {
                "evaluator_version": EVALUATOR_VERSION,
                "cohort_policy_version": COHORT_POLICY_VERSION,
                "limit": limit,
            },
        ).mappings()
        pending = tuple(
            (cast(UUID, row["result_revision_id"]), cast(UUID, row["prediction_id"]))
            for row in rows
        )
        for result_revision_id, prediction_id in pending:
            self._enqueue(result_revision_id, now=now, trigger_prediction_id=prediction_id)
        return len(pending)

    def _enqueue(
        self,
        result_revision_id: UUID,
        *,
        now: datetime,
        trigger_prediction_id: UUID | None = None,
    ) -> Mapping[str, Any]:
        payload = {
            "result_revision_id": str(result_revision_id),
            "evaluator_version": EVALUATOR_VERSION,
            "cohort_policy_version": COHORT_POLICY_VERSION,
        }
        if trigger_prediction_id is not None:
            payload["trigger_prediction_id"] = str(trigger_prediction_id)
        return self._jobs.enqueue(
            job_key=evaluation_job_key(result_revision_id, trigger_prediction_id),
            job_type=EVALUATION_JOB_TYPE,
            payload=payload,
            due_at=now,
            deadline_at=None,
        )


class DatabaseEvaluationTicker:
    """Production scanner called once per worker poll before job dispatch."""

    def __init__(self, engine: Engine, *, batch_size: int = 100) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._engine = engine
        self._batch_size = batch_size

    def run_once(self, *, now: datetime) -> int:
        with self._engine.begin() as connection:
            return EvaluationJobPlanner(connection).enqueue_pending(
                now=now,
                limit=self._batch_size,
            )


class EvaluationJobHandler:
    """Evaluate a leased result job in one short PostgreSQL transaction."""

    def __init__(self, engine: Engine, *, evaluator: ResultEvaluator | None = None) -> None:
        self._engine = engine
        self._evaluator = evaluator

    def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
        del now
        result_revision_id = result_revision_id_from_job(job)
        with self._engine.begin() as connection:
            EvaluationUnitOfWork(connection, evaluator=self._evaluator).evaluate_result(
                result_revision_id
            )


def evaluation_handlers(engine: Engine) -> dict[str, EvaluationJobHandler]:
    """Return the production handler mapping consumed by the worker dispatcher."""

    return {EVALUATION_JOB_TYPE: EvaluationJobHandler(engine)}


def evaluation_job_key(
    result_revision_id: UUID,
    trigger_prediction_id: UUID | None = None,
) -> str:
    prediction_part = f":{trigger_prediction_id}" if trigger_prediction_id is not None else ""
    return (
        f"evaluation:{result_revision_id}{prediction_part}:{EVALUATOR_VERSION}:"
        f"{COHORT_POLICY_VERSION}"
    )


def result_revision_id_from_job(job: Mapping[str, Any]) -> UUID:
    if str(job.get("job_type")) != EVALUATION_JOB_TYPE:
        raise ValueError("job is not a result evaluation job")
    payload = job.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("evaluation job payload must be an object")
    if payload.get("evaluator_version") != EVALUATOR_VERSION:
        raise ValueError("evaluation job evaluator_version is unsupported")
    if payload.get("cohort_policy_version") != COHORT_POLICY_VERSION:
        raise ValueError("evaluation job cohort_policy_version is unsupported")
    try:
        return UUID(str(payload["result_revision_id"]))
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("evaluation job result_revision_id is invalid") from error


def _result_from_row(row: RowMapping) -> ResultRevision:
    return ResultRevision(
        result_revision_id=str(row["id"]),
        match_id=str(row["match_id"]),
        revision=int(row["revision"]),
        finality=str(row["finality"]),
        home_sets=int(row["home_sets"]),
        away_sets=int(row["away_sets"]),
        home_points=int(row["home_points"]),
        away_points=int(row["away_points"]),
    )


def _optional_probability(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("home_win_probability must be numeric")
    return float(value)


def _set_score_probabilities(value: object) -> Mapping[str, float] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(key): _required_probability(item) for key, item in value.items()}
    if not isinstance(value, list):
        raise ValueError("set_score_probabilities must be an array or object")
    result: dict[str, float] = {}
    for raw_item in value:
        item = _object(raw_item, "set score probability")
        outcome = item.get("outcome")
        if not isinstance(outcome, str) or not outcome:
            raise ValueError("set score outcome must be a non-blank string")
        if outcome in result:
            raise ValueError("set score outcomes must be unique")
        result[outcome] = _required_probability(item.get("probability"))
    return result


def _required_probability(value: object) -> float:
    result = _optional_probability(value)
    if result is None:
        raise ValueError("probability must not be null")
    return result


def _market_snapshot_from_row(row: RowMapping) -> MarketSnapshot:
    markets_document = _object(row["markets_json"], "markets_json")
    raw_markets = markets_document.get("markets")
    if not isinstance(raw_markets, list):
        raise ValueError("markets_json.markets must be an array")
    return MarketSnapshot(
        snapshot_id=str(row["market_snapshot_id"]),
        match_id=str(row["match_id"]),
        source=str(row["source"]),
        source_event_id=str(row["source_event_id"]),
        quoted_at=cast(datetime, row["quoted_at"]),
        observed_at=cast(datetime, row["observed_at"]),
        received_at=cast(datetime, row["received_at"]),
        contract_version=str(row["contract_version"]),
        markets=tuple(_market_line_from_document(item) for item in raw_markets),
        sha256=str(row["sha256"]),
    )


def _market_line_from_document(value: object) -> MarketLine:
    item = _object(value, "market line")
    return MarketLine(
        line_id=str(item["line_id"]),
        market_type=MarketType(str(item["market_type"])),
        unit=MarketUnit(str(item["unit"])),
        period=MarketPeriod(str(item["period"])),
        selection=MarketSelection(str(item["selection"])),
        line=Decimal(str(item["line"])) if item.get("line") is not None else None,
        decimal_odds=Decimal(str(item["decimal_odds"])),
        settlement_rule_version=str(item["settlement_rule_version"]),
        source=str(item["source"]),
        quoted_at=_datetime(item["quoted_at"], "quoted_at"),
        observed_at=_datetime(item["observed_at"], "observed_at"),
        received_at=_datetime(item["received_at"], "received_at"),
    )


def _market_line_evaluation(
    line_id: str,
    derived: Mapping[str, Any],
) -> MarketLineEvaluation:
    value = derived.get(line_id)
    if value is None:
        return MarketLineEvaluation(
            line_id=line_id,
            eligibility=EvaluationEligibility.UNSUPPORTED,
            reason="stored_line_was_unsupported",
            probabilities=None,
        )
    probabilities = _object(value, f"derived probability {line_id}")
    return MarketLineEvaluation(
        line_id=line_id,
        eligibility=EvaluationEligibility.ELIGIBLE,
        reason="stored_market_evaluation",
        probabilities=LineProbabilities(
            win=_required_probability(probabilities.get("win")),
            push=_required_probability(probabilities.get("push")),
            loss=_required_probability(probabilities.get("loss")),
        ),
    )


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{name} must be an ISO 8601 datetime") from error
    else:
        raise ValueError(f"{name} must be a datetime")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return result


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return cast(Mapping[str, Any], value)
