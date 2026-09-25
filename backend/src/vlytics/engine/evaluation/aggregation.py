"""Cohort-safe individual and common-match performance aggregation."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from vlytics.engine.evaluation.metrics import (
    BINARY_METRIC_VERSION,
    CALIBRATION_VERSION,
    LOG_LOSS_EPSILON,
    LOG_LOSS_VERSION,
    SET_ACCURACY_VERSION,
    SET_RPS_VERSION,
    CalibrationBin,
    MarketAccuracy,
    PairedDifference,
    calibration_bins,
    market_accuracy,
    paired_difference,
)
from vlytics.engine.evaluation.models import (
    COHORT_POLICY_VERSION,
    CohortKey,
    Evaluation,
    PerformanceObservation,
)

PERFORMANCE_SCHEMA_VERSION = "performance-v1"


@dataclass(frozen=True)
class Coverage:
    scheduled: int
    predicted: int
    result_available: int
    evaluated: int
    missing_predictions: int
    missing_results: int
    coverage: float
    reasons: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduled": self.scheduled,
            "predicted": self.predicted,
            "result_available": self.result_available,
            "evaluated": self.evaluated,
            "missing_predictions": self.missing_predictions,
            "missing_results": self.missing_results,
            "coverage": self.coverage,
            "reasons": dict(sorted(self.reasons.items())),
        }


@dataclass(frozen=True)
class MetricSummary:
    winner_n: int
    set_n: int
    brier: float | None
    log_loss: float | None
    winner_accuracy: float | None
    set_rps: float | None
    set_score_accuracy: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "winner_n": self.winner_n,
            "set_n": self.set_n,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "winner_accuracy": self.winner_accuracy,
            "set_rps": self.set_rps,
            "set_score_accuracy": self.set_score_accuracy,
        }


@dataclass(frozen=True)
class CohortPerformance:
    cohort: CohortKey
    coverage: Coverage
    metrics: MetricSummary
    calibration: tuple[CalibrationBin, ...]
    market: MarketAccuracy

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort.to_dict(),
            "coverage": self.coverage.to_dict(),
            "metrics": self.metrics.to_dict(),
            "calibration": [item.to_dict() for item in self.calibration],
            "market": self.market.to_dict(),
        }


@dataclass(frozen=True)
class ComparisonSpec:
    comparison_id: str
    ai_cohort: CohortKey
    baseline_cohort: CohortKey

    def __post_init__(self) -> None:
        if not self.comparison_id.strip():
            raise ValueError("comparison_id must not be blank")
        dimensions = (
            "division",
            "competition",
            "stage",
            "feature_version",
            "availability_policy",
            "timing_eligibility",
            "result_finality",
        )
        if any(
            getattr(self.ai_cohort, item) != getattr(self.baseline_cohort, item)
            for item in dimensions
        ):
            raise ValueError("paired cohorts must share non-model cohort dimensions")


@dataclass(frozen=True)
class CohortComparison:
    comparison_id: str
    ai_cohort: CohortKey
    baseline_cohort: CohortKey
    ai_individual_n: int
    baseline_individual_n: int
    paired_n: int
    brier: PairedDifference
    log_loss: PairedDifference
    excluded_reasons: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "ai_cohort": self.ai_cohort.to_dict(),
            "baseline_cohort": self.baseline_cohort.to_dict(),
            "ai_individual_n": self.ai_individual_n,
            "baseline_individual_n": self.baseline_individual_n,
            "paired_n": self.paired_n,
            "brier": self.brier.to_dict(),
            "log_loss": self.log_loss.to_dict(),
            "excluded_reasons": dict(sorted(self.excluded_reasons.items())),
        }


@dataclass(frozen=True)
class PerformanceReport:
    generated_at: datetime
    cohorts: tuple[CohortPerformance, ...]
    comparisons: tuple[CohortComparison, ...]
    schema_version: str = PERFORMANCE_SCHEMA_VERSION
    cohort_policy_version: str = COHORT_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat(),
            "cohort_policy_version": self.cohort_policy_version,
            "metric_versions": {
                "binary": BINARY_METRIC_VERSION,
                "log_loss": LOG_LOSS_VERSION,
                "log_loss_epsilon": LOG_LOSS_EPSILON,
                "set_rps": SET_RPS_VERSION,
                "set_score_accuracy": SET_ACCURACY_VERSION,
                "calibration": CALIBRATION_VERSION,
            },
            "cohorts": [item.to_dict() for item in self.cohorts],
            "comparisons": [item.to_dict() for item in self.comparisons],
        }


def aggregate_performance(
    observations: Sequence[PerformanceObservation],
    *,
    generated_at: datetime,
    comparisons: Sequence[ComparisonSpec] = (),
) -> PerformanceReport:
    grouped: dict[CohortKey, list[PerformanceObservation]] = defaultdict(list)
    seen: set[tuple[CohortKey, tuple[str, str, str, datetime]]] = set()
    for observation in observations:
        identity = (observation.cohort, observation.pairing_key)
        if identity in seen:
            raise ValueError("one observation per cohort and pairing key is required")
        seen.add(identity)
        grouped[observation.cohort].append(observation)

    cohort_reports = tuple(
        _cohort_performance(cohort, grouped[cohort]) for cohort in sorted(grouped)
    )
    paired_reports = tuple(_compare(grouped, spec) for spec in comparisons)
    return PerformanceReport(generated_at, cohort_reports, paired_reports)


def _cohort_performance(
    cohort: CohortKey,
    observations: Sequence[PerformanceObservation],
) -> CohortPerformance:
    evaluations = [
        item.evaluation
        for item in observations
        if item.evaluation is not None and item.evaluation.eligible
    ]
    winner = [item for item in evaluations if item.brier is not None]
    sets = [item for item in evaluations if item.set_rps is not None]
    reasons = Counter[str]()
    for item in observations:
        if not item.prediction_available:
            reasons[item.failure_reason or "prediction_missing"] += 1
        if not item.result_available:
            reasons["result_missing"] += 1
        if item.prediction_available and item.result_available and item.evaluation is None:
            reasons["evaluation_missing"] += 1
        elif item.evaluation is not None and not item.evaluation.eligible:
            reasons[item.evaluation.reason] += 1

    scheduled = len(observations)
    predicted = sum(item.prediction_available for item in observations)
    result_available = sum(item.result_available for item in observations)
    evaluated = len(winner)
    coverage = Coverage(
        scheduled=scheduled,
        predicted=predicted,
        result_available=result_available,
        evaluated=evaluated,
        missing_predictions=scheduled - predicted,
        missing_results=scheduled - result_available,
        coverage=evaluated / scheduled if scheduled else 0.0,
        reasons=dict(reasons),
    )
    metrics = MetricSummary(
        winner_n=len(winner),
        set_n=len(sets),
        brier=_mean(item.brier for item in winner),
        log_loss=_mean(item.log_loss for item in winner),
        winner_accuracy=_mean(item.winner_accuracy for item in winner),
        set_rps=_mean(item.set_rps for item in sets),
        set_score_accuracy=_mean(item.set_score_accuracy for item in sets),
    )
    calibration = calibration_bins(
        [
            (item.prediction.home_win_probability, item.result.home_won)
            for item in winner
            if item.prediction.home_win_probability is not None
        ]
    )
    market_outcomes: list[str] = []
    for item in observations:
        if item.evaluation is None:
            continue
        if item.evaluation.market_settlements:
            market_outcomes.extend(
                settlement.outcome.value for settlement in item.evaluation.market_settlements
            )
        elif item.evaluation.market_status in {"missing", "stale", "late", "unsupported"}:
            market_outcomes.append(item.evaluation.market_status)
    return CohortPerformance(
        cohort, coverage, metrics, calibration, market_accuracy(market_outcomes)
    )


def _compare(
    grouped: dict[CohortKey, list[PerformanceObservation]],
    spec: ComparisonSpec,
) -> CohortComparison:
    ai_observations = grouped.get(spec.ai_cohort, ())
    baseline_observations = grouped.get(spec.baseline_cohort, ())
    ai = _eligible_by_pair(ai_observations)
    baseline = _eligible_by_pair(baseline_observations)
    common = sorted(set(ai) & set(baseline), key=str)
    valid_common = [
        key
        for key in common
        if ai[key].result.result_revision_id == baseline[key].result.result_revision_id
    ]
    exclusions = Counter[str]()
    exclusions["ai_unavailable"] = len(set(baseline) - set(ai))
    exclusions["baseline_unavailable"] = len(set(ai) - set(baseline))
    observed_pairs = {item.pairing_key for item in ai_observations} | {
        item.pairing_key for item in baseline_observations
    }
    exclusions["both_unavailable"] = len(observed_pairs - set(ai) - set(baseline))
    exclusions["result_revision_mismatch"] = len(common) - len(valid_common)
    exclusions = Counter({key: value for key, value in exclusions.items() if value})
    brier_values = [
        _required_metric(ai[key], "brier") - _required_metric(baseline[key], "brier")
        for key in valid_common
    ]
    log_values = [
        _required_metric(ai[key], "log_loss") - _required_metric(baseline[key], "log_loss")
        for key in valid_common
    ]
    return CohortComparison(
        comparison_id=spec.comparison_id,
        ai_cohort=spec.ai_cohort,
        baseline_cohort=spec.baseline_cohort,
        ai_individual_n=len(ai),
        baseline_individual_n=len(baseline),
        paired_n=len(valid_common),
        brier=paired_difference(brier_values, metric="brier"),
        log_loss=paired_difference(log_values, metric="log_loss"),
        excluded_reasons=dict(exclusions),
    )


def _eligible_by_pair(
    observations: Sequence[PerformanceObservation],
) -> dict[tuple[str, str, str, datetime], Evaluation]:
    return {
        item.pairing_key: item.evaluation
        for item in observations
        if item.evaluation is not None
        and item.evaluation.eligible
        and item.evaluation.brier is not None
        and item.evaluation.log_loss is not None
    }


def _required_metric(evaluation: Evaluation, name: str) -> float:
    value = getattr(evaluation, name)
    if not isinstance(value, float):
        raise ValueError(f"paired evaluation is missing {name}")
    return value


def _mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None
