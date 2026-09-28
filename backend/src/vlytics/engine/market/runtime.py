"""Durable post-prediction Market comparison workflow."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import Connection, Engine, RowMapping, text

from vlytics.engine.market.adapters import MarketAdapter, default_market_adapter
from vlytics.engine.market.evaluator import EVALUATOR_VERSION, MarketEvaluator
from vlytics.engine.market.models import (
    MarketContractError,
    MarketEvaluation,
    PredictionMarketInput,
)
from vlytics.engine.market.repository import MarketEvaluationRepository
from vlytics.ops.repositories import JobRepository
from vlytics.ops.scheduler import TerminalJobError

MARKET_COMPARISON_JOB_TYPE = "engine.compare_market"
_SET_OUTCOMES = ("3:0", "3:1", "3:2", "2:3", "1:3", "0:3")


class PredictionMarketReader(Protocol):
    def load(self, prediction_id: UUID) -> PredictionMarketInput: ...


class MarketEvaluationWriter(Protocol):
    def add_evaluation(self, evaluation: MarketEvaluation) -> UUID: ...


@dataclass(frozen=True)
class MarketComparisonSummary:
    prediction_id: UUID
    evaluation_id: UUID
    eligibility: str
    market_snapshot_id: str | None


class MarketComparisonService:
    """Compare one immutable prediction with Market state at its exact cutoff."""

    def __init__(
        self,
        reader: PredictionMarketReader,
        adapter: MarketAdapter,
        writer: MarketEvaluationWriter,
        *,
        max_age: timedelta,
        evaluator: MarketEvaluator | None = None,
    ) -> None:
        if max_age < timedelta(0):
            raise ValueError("max_age must be non-negative")
        self._reader = reader
        self._adapter = adapter
        self._writer = writer
        self._max_age = max_age
        self._evaluator = evaluator or MarketEvaluator()

    def compare_prediction(self, prediction_id: UUID) -> MarketComparisonSummary:
        prediction = self._reader.load(prediction_id)
        availability = self._adapter.snapshot_as_of(
            match_id=prediction.match_id,
            cutoff_at=prediction.input_cutoff_at,
            max_age=self._max_age,
        )
        evaluation = self._evaluator.evaluate(prediction, availability)
        evaluation_id = self._writer.add_evaluation(evaluation)
        return MarketComparisonSummary(
            prediction_id=prediction_id,
            evaluation_id=evaluation_id,
            eligibility=evaluation.eligibility.value,
            market_snapshot_id=evaluation.snapshot_id,
        )


class PostgresPredictionMarketReader:
    """Rebuild a prediction only from hash-verified append-only artifacts."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def load(self, prediction_id: UUID) -> PredictionMarketInput:
        row = (
            self._connection.execute(
                text(
                    """
                    SELECT p.id, p.match_id, p.snapshot_id, p.variant_id,
                           p.input_cutoff_at, p.output_json,
                           p.sha256 AS prediction_sha256,
                           v.hyperparameters,
                           joint.distribution_id,
                           joint.match_id AS joint_match_id,
                           joint.snapshot_id AS joint_snapshot_id,
                           joint.variant_id AS joint_variant_id,
                           joint.artifact_json,
                           joint.sha256 AS joint_sha256
                    FROM engine.predictions p
                    JOIN engine.prediction_status_projection status
                      ON status.prediction_id = p.id
                     AND status.current_status = 'published'
                    JOIN engine.model_variants v ON v.id = p.variant_id
                    LEFT JOIN engine.joint_score_distributions joint
                      ON joint.distribution_id =
                         p.output_json ->> 'joint_score_distribution_ref'
                    WHERE p.id = :prediction_id
                    """
                ),
                {"prediction_id": prediction_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError(f"published prediction {prediction_id} does not exist")
        return self._from_row(row)

    @staticmethod
    def _from_row(row: RowMapping) -> PredictionMarketInput:
        output = _object(row["output_json"], "prediction output_json")
        if _canonical_sha256(output) != str(row["prediction_sha256"]):
            raise MarketContractError("prediction output hash is inconsistent")
        hyperparameters = _object(row["hyperparameters"], "variant hyperparameters")
        variant_key = _string(hyperparameters, "variant_key")
        if _string(output, "producer_variant_id") != variant_key:
            raise MarketContractError("prediction output belongs to another variant")

        snapshot_id = str(row["snapshot_id"])
        joint_reference = output.get("joint_score_distribution_ref")
        point_totals: Mapping[int, float] | None = None
        point_differentials: Mapping[int, float] | None = None
        if joint_reference is not None:
            if not isinstance(joint_reference, str):
                raise MarketContractError("joint score reference must be a string")
            if row["distribution_id"] is None or row["artifact_json"] is None:
                raise MarketContractError("joint score distribution was not found")
            if str(row["distribution_id"]) != joint_reference:
                raise MarketContractError("joint score distribution ID is inconsistent")
            if (
                str(row["joint_match_id"]) != str(row["match_id"])
                or str(row["joint_snapshot_id"]) != snapshot_id
                or str(row["joint_variant_id"]) != str(row["variant_id"])
            ):
                raise MarketContractError("joint score distribution ownership is inconsistent")
            artifact = _object(row["artifact_json"], "joint score artifact")
            if _canonical_sha256(artifact) != str(row["joint_sha256"]):
                raise MarketContractError("joint score artifact hash is inconsistent")
            if _string(artifact, "distribution_id") != joint_reference:
                raise MarketContractError("joint score artifact identity is inconsistent")
            ownership = _object(artifact.get("ownership"), "joint score ownership")
            if (
                _string(ownership, "producer_variant_id") != variant_key
                or _string(ownership, "input_snapshot_id") != snapshot_id
            ):
                raise MarketContractError("joint score artifact belongs to another prediction")
            marginals = _object(artifact.get("marginals"), "joint score marginals")
            point_totals = _point_distribution(
                marginals.get("point_totals"),
                value_key="total_points",
                name="point_totals",
            )
            point_differentials = _point_distribution(
                marginals.get("point_differentials"),
                value_key="point_differential",
                name="point_differentials",
            )
            _verify_joint_marginals(output, marginals)

        return PredictionMarketInput.from_persisted_marginals(
            prediction_id=str(row["id"]),
            match_id=str(row["match_id"]),
            input_cutoff_at=cast(datetime, row["input_cutoff_at"]),
            joint_score_distribution_id=(
                str(joint_reference) if joint_reference is not None else None
            ),
            producer_variant_id=variant_key,
            input_snapshot_id=snapshot_id,
            home_win_probability=_optional_probability(output.get("home_win_probability")),
            set_score_probabilities=_set_score_probabilities(output.get("set_score_probabilities")),
            point_totals=point_totals,
            point_differentials=point_differentials,
        )


class MarketComparisonUnitOfWork:
    """Commit one prediction comparison atomically."""

    def __init__(
        self,
        connection: Connection,
        adapter: MarketAdapter,
        *,
        max_age: timedelta,
    ) -> None:
        self._service = MarketComparisonService(
            PostgresPredictionMarketReader(connection),
            adapter,
            MarketEvaluationRepository(connection),
            max_age=max_age,
        )

    def compare_prediction(self, prediction_id: UUID) -> MarketComparisonSummary:
        return self._service.compare_prediction(prediction_id)


class MarketComparisonJobPlanner:
    """Create one versioned Market comparison job per published prediction."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._jobs = JobRepository(connection)

    def enqueue_pending(self, *, now: datetime, limit: int = 100) -> int:
        if limit < 1:
            raise ValueError("limit must be positive")
        prediction_ids = tuple(
            cast(UUID, row["prediction_id"])
            for row in self._connection.execute(
                text(
                    """
                    SELECT prediction.id AS prediction_id
                    FROM engine.predictions prediction
                    JOIN engine.prediction_status_projection status
                      ON status.prediction_id = prediction.id
                     AND status.current_status = 'published'
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM market.market_evaluations evaluation
                        WHERE evaluation.prediction_id = prediction.id
                          AND evaluation.evaluator_version = :evaluator_version
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM ops.jobs job
                        WHERE job.job_key = concat(
                            'market-comparison:', prediction.id::text, ':',
                            :evaluator_version
                        )
                    )
                    ORDER BY prediction.generated_at, prediction.id
                    LIMIT :limit
                    """
                ),
                {"evaluator_version": EVALUATOR_VERSION, "limit": limit},
            ).mappings()
        )
        for prediction_id in prediction_ids:
            self._jobs.enqueue(
                job_key=market_comparison_job_key(prediction_id),
                job_type=MARKET_COMPARISON_JOB_TYPE,
                payload={
                    "prediction_id": str(prediction_id),
                    "evaluator_version": EVALUATOR_VERSION,
                },
                due_at=now,
                deadline_at=None,
            )
        return len(prediction_ids)


class DatabaseMarketComparisonTicker:
    """Production scanner called once per worker poll after prediction discovery."""

    def __init__(self, engine: Engine, *, batch_size: int = 100) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._engine = engine
        self._batch_size = batch_size

    def run_once(self, *, now: datetime) -> int:
        with self._engine.begin() as connection:
            return MarketComparisonJobPlanner(connection).enqueue_pending(
                now=now,
                limit=self._batch_size,
            )


MarketAdapterFactory = Callable[[Connection], MarketAdapter]


class MarketComparisonJobHandler:
    """Execute a leased comparison job in one short PostgreSQL transaction."""

    def __init__(
        self,
        engine: Engine,
        *,
        adapter_factory: MarketAdapterFactory | None = None,
        max_age: timedelta | None = None,
    ) -> None:
        self._engine = engine
        self._adapter_factory: MarketAdapterFactory
        if adapter_factory is None:
            self._adapter_factory = _missing_market_adapter
            self._max_age = timedelta(0)
        else:
            if max_age is None:
                raise ValueError("configured Market adapters require an explicit max_age policy")
            self._adapter_factory = adapter_factory
            self._max_age = max_age

    def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
        del now
        try:
            prediction_id = prediction_id_from_market_job(job)
        except ValueError as error:
            raise TerminalJobError("invalid_market_comparison_job") from error
        try:
            with self._engine.begin() as connection:
                MarketComparisonUnitOfWork(
                    connection,
                    self._adapter_factory(connection),
                    max_age=self._max_age,
                ).compare_prediction(prediction_id)
        except (LookupError, MarketContractError) as error:
            raise TerminalJobError("ineligible_market_prediction") from error


def market_handlers(
    engine: Engine,
    *,
    adapter_factory: MarketAdapterFactory | None = None,
    max_age: timedelta | None = None,
) -> dict[str, MarketComparisonJobHandler]:
    return {
        MARKET_COMPARISON_JOB_TYPE: MarketComparisonJobHandler(
            engine,
            adapter_factory=adapter_factory,
            max_age=max_age,
        )
    }


def market_comparison_job_key(prediction_id: UUID) -> str:
    return f"market-comparison:{prediction_id}:{EVALUATOR_VERSION}"


def prediction_id_from_market_job(job: Mapping[str, Any]) -> UUID:
    if str(job.get("job_type")) != MARKET_COMPARISON_JOB_TYPE:
        raise ValueError("job is not a Market comparison job")
    payload = job.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("Market comparison job payload must be an object")
    if payload.get("evaluator_version") != EVALUATOR_VERSION:
        raise ValueError("Market comparison evaluator_version is unsupported")
    try:
        return UUID(str(payload["prediction_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Market comparison prediction_id is invalid") from error


def _missing_market_adapter(_connection: Connection) -> MarketAdapter:
    return default_market_adapter()


def _verify_joint_marginals(
    output: Mapping[str, Any],
    marginals: Mapping[str, Any],
) -> None:
    output_winner = _optional_probability(output.get("home_win_probability"))
    artifact_winner = _optional_probability(marginals.get("home_win_probability"))
    winner_mismatch = (
        output_winner is None
        or artifact_winner is None
        or abs(output_winner - artifact_winner) > 1e-6
    )
    if winner_mismatch:
        raise MarketContractError("joint score winner marginal is inconsistent")
    output_sets = _set_score_probabilities(output.get("set_score_probabilities"))
    artifact_sets = _set_score_probabilities(marginals.get("set_score_probabilities"))
    if (
        output_sets is None
        or artifact_sets is None
        or any(
            abs(output_sets[outcome] - artifact_sets[outcome]) > 1e-6 for outcome in _SET_OUTCOMES
        )
    ):
        raise MarketContractError("joint score set marginal is inconsistent")


def _set_score_probabilities(value: object) -> dict[str, float] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        result = {str(key): _required_probability(item) for key, item in value.items()}
    elif isinstance(value, list):
        result = {}
        for raw_item in value:
            item = _object(raw_item, "set score probability")
            outcome = _string(item, "outcome")
            if outcome in result:
                raise MarketContractError("set score outcomes must be unique")
            result[outcome] = _required_probability(item.get("probability"))
    else:
        raise MarketContractError("set_score_probabilities must be an array or object")
    if set(result) != set(_SET_OUTCOMES):
        raise MarketContractError("set score distribution has invalid outcomes")
    return result


def _point_distribution(
    value: object,
    *,
    value_key: str,
    name: str,
) -> dict[int, float]:
    if not isinstance(value, list):
        raise MarketContractError(f"{name} must be an array")
    result: dict[int, float] = {}
    for raw_item in value:
        item = _object(raw_item, name)
        raw_point = item.get(value_key)
        if isinstance(raw_point, bool) or not isinstance(raw_point, int):
            raise MarketContractError(f"{name}.{value_key} must be an integer")
        if raw_point in result:
            raise MarketContractError(f"{name} values must be unique")
        result[raw_point] = _required_probability(item.get("probability"))
    return result


def _optional_probability(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MarketContractError("probability must be numeric")
    result = float(value)
    if not 0 <= result <= 1:
        raise MarketContractError("probability must be between zero and one")
    return result


def _required_probability(value: object) -> float:
    result = _optional_probability(value)
    if result is None:
        raise MarketContractError("probability must not be null")
    return result


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MarketContractError(f"{name} must be an object")
    return cast(Mapping[str, Any], value)


def _string(value: Mapping[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise MarketContractError(f"{name} must be a non-blank string")
    return item


def _canonical_sha256(document: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
