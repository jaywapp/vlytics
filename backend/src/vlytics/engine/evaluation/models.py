"""Immutable input and output models for result evaluation and performance cohorts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Any

from vlytics.engine.evaluation.metrics import SET_SCORE_HOME_ORDER
from vlytics.engine.market import MarketSettlement

COHORT_POLICY_VERSION = "performance-cohort-v1"
EVALUATOR_VERSION = "result-evaluator-v1"


@dataclass(frozen=True, order=True)
class CohortKey:
    division: str
    competition: str
    stage: str
    provider: str
    model_version: str
    prompt_version: str
    feature_version: str
    availability_policy: str
    timing_eligibility: str
    result_finality: str

    def __post_init__(self) -> None:
        if self.division not in {"men", "women"}:
            raise ValueError("division must be men or women")
        if self.stage not in {"regular", "playoff", "championship", "other"}:
            raise ValueError("stage is not supported")
        if self.availability_policy not in {
            "historical_reconstruction",
            "historical_point_in_time",
            "live_prospective",
        }:
            raise ValueError("availability_policy is not supported")
        if self.timing_eligibility not in {"on_time", "reconstructed", "diagnostic"}:
            raise ValueError("timing_eligibility is not supported")
        if self.result_finality not in {"provisional", "final", "corrected", "void"}:
            raise ValueError("result_finality is not supported")
        for name in (
            "competition",
            "provider",
            "model_version",
            "prompt_version",
            "feature_version",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")

    def to_dict(self) -> dict[str, str]:
        return {
            "division": self.division,
            "competition": self.competition,
            "stage": self.stage,
            "provider": self.provider,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "feature_version": self.feature_version,
            "availability_policy": self.availability_policy,
            "timing_eligibility": self.timing_eligibility,
            "result_finality": self.result_finality,
        }


@dataclass(frozen=True)
class PredictionEvaluationInput:
    prediction_id: str
    match_id: str
    schedule_revision_id: str
    snapshot_id: str
    input_cutoff_at: datetime
    cohort: CohortKey
    home_win_probability: float | None
    set_score_probabilities: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        for name in ("prediction_id", "match_id", "schedule_revision_id", "snapshot_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        if self.input_cutoff_at.tzinfo is None:
            raise ValueError("input_cutoff_at must be timezone-aware")
        if self.home_win_probability is not None and not 0 <= self.home_win_probability <= 1:
            raise ValueError("home_win_probability must be between zero and one")
        if self.set_score_probabilities is not None:
            values = dict(self.set_score_probabilities)
            if set(values) != set(SET_SCORE_HOME_ORDER):
                raise ValueError("set_score_probabilities must contain six supported outcomes")
            if any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or not 0 <= value <= 1
                for value in values.values()
            ):
                raise ValueError("set_score_probabilities must be finite probabilities")
            if abs(sum(values.values()) - 1.0) > 1e-6:
                raise ValueError("set_score_probabilities must sum to one")
            object.__setattr__(self, "set_score_probabilities", MappingProxyType(values))

    @property
    def pairing_key(self) -> tuple[str, str, str, datetime]:
        return (
            self.match_id,
            self.schedule_revision_id,
            self.snapshot_id,
            self.input_cutoff_at,
        )


@dataclass(frozen=True)
class ResultRevision:
    result_revision_id: str
    match_id: str
    revision: int
    finality: str
    home_sets: int
    away_sets: int
    home_points: int
    away_points: int

    def __post_init__(self) -> None:
        if not self.result_revision_id.strip() or not self.match_id.strip():
            raise ValueError("result revision identity must not be blank")
        if self.revision < 1:
            raise ValueError("result revision must be positive")
        if self.finality not in {"provisional", "final", "corrected", "void"}:
            raise ValueError("result finality is not supported")
        scores = (self.home_sets, self.away_sets, self.home_points, self.away_points)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in scores
        ):
            raise ValueError("result scores must be non-negative integers")
        if self.finality != "void" and (self.home_sets, self.away_sets) not in {
            (3, 0),
            (3, 1),
            (3, 2),
            (2, 3),
            (1, 3),
            (0, 3),
        }:
            raise ValueError("result set score is not a supported completed match")

    @property
    def home_won(self) -> bool:
        return self.home_sets > self.away_sets

    @property
    def set_score(self) -> str:
        return f"{self.home_sets}:{self.away_sets}"


@dataclass(frozen=True)
class Evaluation:
    prediction: PredictionEvaluationInput
    result: ResultRevision
    evaluator_version: str
    cohort_policy_version: str
    eligible: bool
    reason: str
    brier: float | None
    log_loss: float | None
    winner_accuracy: float | None
    set_rps: float | None
    set_score_accuracy: float | None
    market_status: str = "not_requested"
    market_settlements: tuple[MarketSettlement, ...] = ()

    def __post_init__(self) -> None:
        if self.prediction.match_id != self.result.match_id:
            raise ValueError("prediction and result match IDs differ")
        if self.prediction.cohort.result_finality != self.result.finality:
            raise ValueError("cohort finality must match the exact result revision")
        if not self.evaluator_version.strip() or not self.cohort_policy_version.strip():
            raise ValueError("evaluation versions must not be blank")
        if not self.reason.strip():
            raise ValueError("evaluation reason must not be blank")
        if self.market_status not in {
            "not_requested",
            "eligible",
            "missing",
            "stale",
            "late",
            "unsupported",
        }:
            raise ValueError("market_status is not supported")

    @property
    def pairing_key(self) -> tuple[str, str, str, datetime]:
        return self.prediction.pairing_key

    def metric_values(self) -> dict[str, Any]:
        return {
            "evaluation_input": {
                "prediction_id": self.prediction.prediction_id,
                "match_id": self.prediction.match_id,
                "schedule_revision_id": self.prediction.schedule_revision_id,
                "snapshot_id": self.prediction.snapshot_id,
                "input_cutoff_at": self.prediction.input_cutoff_at.isoformat(),
                "result_revision_id": self.result.result_revision_id,
                "result_revision": self.result.revision,
                "result_finality": self.result.finality,
            },
            "eligible": self.eligible,
            "reason": self.reason,
            "home_win_probability": self.prediction.home_win_probability,
            "home_win_outcome": int(self.result.home_won)
            if self.result.finality != "void"
            else None,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "winner_accuracy": self.winner_accuracy,
            "set_rps": self.set_rps,
            "set_score_accuracy": self.set_score_accuracy,
            "actual_set_score": self.result.set_score if self.result.finality != "void" else None,
            "cohort": self.prediction.cohort.to_dict(),
        }

    def settlement_values(self) -> dict[str, Any]:
        return {
            "market_status": self.market_status,
            "market": [
                {
                    "market_snapshot_id": item.snapshot_id,
                    "line_id": item.line_id,
                    "result_revision_id": item.result_revision_id,
                    "evaluator_version": item.evaluator_version,
                    "outcome": item.outcome.value,
                    "reason": item.reason,
                }
                for item in self.market_settlements
            ],
        }


@dataclass(frozen=True)
class PerformanceObservation:
    match_id: str
    pairing_key: tuple[str, str, str, datetime]
    cohort: CohortKey
    prediction_available: bool
    result_available: bool
    evaluation: Evaluation | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.match_id.strip():
            raise ValueError("match_id must not be blank")
        if self.evaluation is not None:
            if self.evaluation.prediction.cohort != self.cohort:
                raise ValueError("observation and evaluation cohorts differ")
            if self.evaluation.pairing_key != self.pairing_key:
                raise ValueError("observation and evaluation pairing keys differ")
            if not self.prediction_available or not self.result_available:
                raise ValueError("an evaluation requires prediction and result availability")
        if self.failure_reason is not None and not self.failure_reason.strip():
            raise ValueError("failure_reason must not be blank")
