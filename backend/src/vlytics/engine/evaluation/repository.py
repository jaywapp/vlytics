"""Append-only, replay-safe persistence for result-revision evaluations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Connection, text

from vlytics.engine.evaluation.models import Evaluation
from vlytics.engine.market import MarketEvaluationRepository


class EvaluationConflictError(RuntimeError):
    """An evaluation identity already exists with different immutable content."""


@dataclass(frozen=True)
class StoredEvaluation:
    id: UUID
    created: bool


class EvaluationRepository:
    """Store one immutable row per prediction, result revision, and policy versions."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(self, evaluation: Evaluation) -> StoredEvaluation:
        metrics = evaluation.metric_values()
        settlement = evaluation.settlement_values()
        parameters = {
            "match_id": UUID(evaluation.prediction.match_id),
            "prediction_id": UUID(evaluation.prediction.prediction_id),
            "result_revision_id": UUID(evaluation.result.result_revision_id),
            "evaluator_version": evaluation.evaluator_version,
            "cohort_policy_version": evaluation.cohort_policy_version,
            "metric_values": _json(metrics),
            "settlement": _json(settlement),
        }
        evaluation_id = self._connection.execute(
            text(
                """
                INSERT INTO engine.evaluations (
                    match_id, prediction_id, result_revision_id, evaluator_version,
                    cohort_policy_version, metric_values, settlement
                ) VALUES (
                    :match_id, :prediction_id, :result_revision_id, :evaluator_version,
                    :cohort_policy_version, CAST(:metric_values AS jsonb),
                    CAST(:settlement AS jsonb)
                )
                ON CONFLICT (
                    prediction_id, result_revision_id, evaluator_version, cohort_policy_version
                ) DO NOTHING
                RETURNING id
                """
            ),
            parameters,
        ).scalar_one_or_none()
        if evaluation_id is not None:
            return StoredEvaluation(cast(UUID, evaluation_id), True)

        existing = self._connection.execute(
            text(
                """
                SELECT id, match_id, metric_values, settlement
                FROM engine.evaluations
                WHERE prediction_id = :prediction_id
                  AND result_revision_id = :result_revision_id
                  AND evaluator_version = :evaluator_version
                  AND cohort_policy_version = :cohort_policy_version
                """
            ),
            parameters,
        ).one()
        if (
            existing[1] != parameters["match_id"]
            or dict(existing[2]) != metrics
            or dict(existing[3]) != settlement
        ):
            raise EvaluationConflictError(
                "existing evaluation has different immutable metric or settlement content"
            )
        return StoredEvaluation(cast(UUID, existing[0]), False)


class EvaluationStore:
    """Persist a general evaluation and only its eligibility-approved Market settlements."""

    def __init__(self, connection: Connection) -> None:
        self.evaluations = EvaluationRepository(connection)
        self.markets = MarketEvaluationRepository(connection)

    def add(self, evaluation: Evaluation) -> StoredEvaluation:
        stored = self.evaluations.add(evaluation)
        if evaluation.market_status == "eligible":
            for settlement in evaluation.market_settlements:
                self.markets.add_settlement(settlement)
        elif evaluation.market_settlements:
            raise EvaluationConflictError("ineligible Market state cannot persist settlements")
        return stored


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
