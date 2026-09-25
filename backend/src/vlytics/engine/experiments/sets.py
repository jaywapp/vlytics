"""Chronological evaluation for the independent-set outcome model."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from types import MappingProxyType
from typing import Any

from vlytics.engine.experiments.baselines import (
    CohortKey,
    TemporalSplit,
    walk_forward_predictions,
)
from vlytics.engine.predictors.elo import EloConfig, EloMatch, EloPrediction
from vlytics.engine.predictors.sets import (
    SET_OUTCOME_ORDER,
    SetModelConfig,
    SetModelParameters,
    SetOutcome,
    SetOutcomePrediction,
    SetOutcomePredictor,
    SetTrainingCohort,
)

SET_METRIC_VERSION = "set-outcome-metrics-v2"
EXACT_SCORE_TIE_POLICY_VERSION = "fractional-tied-maxima-v1"
CALIBRATION_INTERVAL_VERSION = "wilson-95-v1"
CALIBRATION_INTERVAL_Z = 1.959963984540054
CALIBRATION_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


@dataclass(frozen=True)
class CalibrationBin:
    lower_bound: float
    upper_bound: float
    includes_upper_bound: bool
    mean_probability: float
    observed_home_win_rate: float
    uncertainty_lower: float
    uncertainty_upper: float
    interval_version: str
    sample_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "includes_upper_bound": self.includes_upper_bound,
            "interval_version": self.interval_version,
            "lower_bound": self.lower_bound,
            "mean_probability": self.mean_probability,
            "observed_home_win_rate": self.observed_home_win_rate,
            "sample_size": self.sample_size,
            "uncertainty_lower": self.uncertainty_lower,
            "uncertainty_upper": self.uncertainty_upper,
            "upper_bound": self.upper_bound,
        }


@dataclass(frozen=True)
class OutcomeFrequencyError:
    outcome: SetOutcome
    mean_predicted_probability: float
    observed_frequency: float
    residual: float
    sample_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean_predicted_probability": self.mean_predicted_probability,
            "observed_frequency": self.observed_frequency,
            "outcome": self.outcome.value,
            "residual": self.residual,
            "sample_size": self.sample_size,
        }


@dataclass(frozen=True)
class IndependenceAssumptionError:
    mean_absolute_frequency_error: float | None
    maximum_absolute_frequency_error: float | None
    outcomes: tuple[OutcomeFrequencyError, ...]
    note: str = (
        "Final-score residuals measure fit error from the independent-set approximation; "
        "they do not prove that independence is the only source of error."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "maximum_absolute_frequency_error": self.maximum_absolute_frequency_error,
            "mean_absolute_frequency_error": self.mean_absolute_frequency_error,
            "note": self.note,
            "outcomes": [item.to_dict() for item in self.outcomes],
        }


@dataclass(frozen=True)
class SetMetricReport:
    metric_version: str
    exact_score_tie_policy_version: str
    calibration_interval_version: str
    scheduled: int
    predicted: int
    result_available: int
    evaluated: int
    missing_predictions: int
    missing_results: int
    coverage: float
    ranked_probability_score: float | None
    set_score_accuracy: float | None
    winner_calibration: tuple[CalibrationBin, ...]
    independence_assumption_error: IndependenceAssumptionError
    missing_reasons: MappingProxyType[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibration_interval_version": self.calibration_interval_version,
            "coverage": self.coverage,
            "evaluated": self.evaluated,
            "exact_score_tie_policy_version": self.exact_score_tie_policy_version,
            "independence_assumption_error": self.independence_assumption_error.to_dict(),
            "metric_version": self.metric_version,
            "missing_predictions": self.missing_predictions,
            "missing_reasons": dict(self.missing_reasons),
            "missing_results": self.missing_results,
            "predicted": self.predicted,
            "ranked_probability_score": self.ranked_probability_score,
            "result_available": self.result_available,
            "scheduled": self.scheduled,
            "set_score_accuracy": self.set_score_accuracy,
            "winner_calibration": [item.to_dict() for item in self.winner_calibration],
        }


@dataclass(frozen=True)
class SetHoldoutReport:
    cohort: CohortKey
    split: TemporalSplit
    elo_config: EloConfig
    validation_parameters: SetModelParameters
    test_parameters: SetModelParameters
    validation: SetMetricReport
    test: SetMetricReport
    validation_training_ends_before: datetime
    test_training_ends_before: datetime
    test_parameters_frozen: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort.to_dict(),
            "elo_config": self.elo_config.to_dict(),
            "elo_config_id": self.elo_config.config_id,
            "split": self.split.to_dict(),
            "test": self.test.to_dict(),
            "test_parameters": self.test_parameters.to_dict(),
            "test_parameters_frozen": self.test_parameters_frozen,
            "test_training_ends_before": self.test_training_ends_before.isoformat(),
            "validation": self.validation.to_dict(),
            "validation_parameters": self.validation_parameters.to_dict(),
            "validation_training_ends_before": self.validation_training_ends_before.isoformat(),
        }


@dataclass(frozen=True)
class SetHoldoutSuiteReport:
    reports: tuple[SetHoldoutReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"reports": [report.to_dict() for report in self.reports]}


def evaluate_set_predictions(
    matches: Sequence[EloMatch],
    predictions: Sequence[SetOutcomePrediction | None],
) -> SetMetricReport:
    if len(matches) != len(predictions):
        raise ValueError("matches and predictions must have equal lengths")
    rps_values: list[float] = []
    exact_scores: list[float] = []
    calibration_inputs: list[tuple[float, float]] = []
    predicted_totals = [0.0] * len(SET_OUTCOME_ORDER)
    observed_counts = [0] * len(SET_OUTCOME_ORDER)
    predicted = 0
    missing = Counter[str]()

    for match, prediction in zip(matches, predictions, strict=True):
        result = _result_outcome(match)
        if result is None:
            missing["result_unavailable"] += 1
        if prediction is None:
            missing["prediction_unavailable"] += 1
            continue
        _validate_set_prediction_identity(match, prediction)
        predicted += 1
        if result is None:
            continue
        probabilities = prediction.distribution.probabilities
        actual_index = SET_OUTCOME_ORDER.index(result)
        exact_scores.append(_fractional_exact_score(probabilities, actual_index))
        rps_values.append(_ranked_probability_score(probabilities, actual_index))
        calibration_inputs.append((prediction.home_win_probability, float(actual_index < 3)))
        observed_counts[actual_index] += 1
        for index, probability in enumerate(probabilities):
            predicted_totals[index] += probability

    scheduled = len(matches)
    evaluated = len(rps_values)
    result_available = sum(_result_outcome(match) is not None for match in matches)
    return SetMetricReport(
        metric_version=SET_METRIC_VERSION,
        exact_score_tie_policy_version=EXACT_SCORE_TIE_POLICY_VERSION,
        calibration_interval_version=CALIBRATION_INTERVAL_VERSION,
        scheduled=scheduled,
        predicted=predicted,
        result_available=result_available,
        evaluated=evaluated,
        missing_predictions=scheduled - predicted,
        missing_results=scheduled - result_available,
        coverage=evaluated / scheduled if scheduled else 0.0,
        ranked_probability_score=(sum(rps_values) / evaluated if evaluated else None),
        set_score_accuracy=(sum(exact_scores) / evaluated if evaluated else None),
        winner_calibration=_calibration(calibration_inputs),
        independence_assumption_error=_independence_error(
            predicted_totals,
            observed_counts,
            evaluated,
        ),
        missing_reasons=MappingProxyType(dict(sorted(missing.items()))),
    )


def run_set_holdout(
    matches: Sequence[EloMatch],
    split: TemporalSplit,
    elo_config: EloConfig,
    config: SetModelConfig | None = None,
) -> SetHoldoutReport:
    """Generate identity-safe Elo artifacts and evaluate one chronological cohort."""

    ordered = _ordered_unique(matches)
    cohort = _single_cohort(ordered)
    train, validation, test = _partition(ordered, split)
    if not train or not validation or not test:
        raise ValueError("train, validation, and test periods must each contain matches")
    upstream = walk_forward_predictions(ordered, elo_config)

    validation_predictor = SetOutcomePredictor.fit(
        train,
        config,
        known_at=split.train_end,
    )
    validation_predictions = _predict(validation_predictor, validation, upstream)

    test_predictor = SetOutcomePredictor.fit(
        train + validation,
        config,
        known_at=split.validation_end,
    )
    test_predictions = _predict(test_predictor, test, upstream)
    return SetHoldoutReport(
        cohort=cohort,
        split=split,
        elo_config=elo_config,
        validation_parameters=validation_predictor.parameters,
        test_parameters=test_predictor.parameters,
        validation=evaluate_set_predictions(validation, validation_predictions),
        test=evaluate_set_predictions(test, test_predictions),
        validation_training_ends_before=split.train_end,
        test_training_ends_before=split.validation_end,
    )


def run_set_holdout_suite(
    matches: Sequence[EloMatch],
    split: TemporalSplit,
    elo_config: EloConfig,
    config: SetModelConfig | None = None,
) -> SetHoldoutSuiteReport:
    grouped: defaultdict[CohortKey, list[EloMatch]] = defaultdict(list)
    for match in matches:
        grouped[CohortKey.from_match(match)].append(match)
    reports = tuple(
        run_set_holdout(grouped[cohort], split, elo_config, config) for cohort in sorted(grouped)
    )
    return SetHoldoutSuiteReport(reports)


def _predict(
    predictor: SetOutcomePredictor,
    matches: Sequence[EloMatch],
    upstream: dict[str, EloPrediction],
) -> list[SetOutcomePrediction]:
    return [predictor.predict(match, upstream[match.match_id]) for match in matches]


def _validate_set_prediction_identity(
    match: EloMatch,
    prediction: SetOutcomePrediction,
) -> None:
    mismatched: list[str] = []
    if prediction.match_id != match.match_id:
        mismatched.append("match_id")
    if prediction.schedule_revision_id != match.schedule_revision_id:
        mismatched.append("schedule_revision_id")
    if prediction.prediction_cutoff_at != match.prediction_cutoff_at:
        mismatched.append("prediction_cutoff_at")
    if prediction.division is not match.division:
        mismatched.append("division")
    if prediction.training_cohort != SetTrainingCohort.from_match(match):
        mismatched.append("cohort")
    if mismatched:
        raise ValueError("set prediction identity mismatch: " + ", ".join(mismatched))


def _fractional_exact_score(probabilities: tuple[float, ...], actual_index: int) -> float:
    maximum = max(probabilities)
    tied = tuple(index for index, value in enumerate(probabilities) if value == maximum)
    return 1.0 / len(tied) if actual_index in tied else 0.0


def _ranked_probability_score(
    home_first_probabilities: tuple[float, ...],
    actual_home_first_index: int,
) -> float:
    probabilities = tuple(reversed(home_first_probabilities))
    actual_index = len(home_first_probabilities) - 1 - actual_home_first_index
    predicted_cumulative = 0.0
    observed_cumulative = 0.0
    total = 0.0
    for index in range(len(probabilities) - 1):
        predicted_cumulative += probabilities[index]
        observed_cumulative += float(index == actual_index)
        total += (predicted_cumulative - observed_cumulative) ** 2
    return total / (len(probabilities) - 1)


def _calibration(inputs: Sequence[tuple[float, float]]) -> tuple[CalibrationBin, ...]:
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(len(CALIBRATION_EDGES) - 1)]
    for probability, outcome in inputs:
        index = min(int(probability * (len(CALIBRATION_EDGES) - 1)), len(buckets) - 1)
        buckets[index].append((probability, outcome))
    bins: list[CalibrationBin] = []
    for index, values in enumerate(buckets):
        if not values:
            continue
        successes = sum(item[1] for item in values)
        lower, upper = _wilson_interval(successes, len(values))
        bins.append(
            CalibrationBin(
                lower_bound=CALIBRATION_EDGES[index],
                upper_bound=CALIBRATION_EDGES[index + 1],
                includes_upper_bound=index == len(buckets) - 1,
                mean_probability=sum(item[0] for item in values) / len(values),
                observed_home_win_rate=successes / len(values),
                uncertainty_lower=lower,
                uncertainty_upper=upper,
                interval_version=CALIBRATION_INTERVAL_VERSION,
                sample_size=len(values),
            )
        )
    return tuple(bins)


def _wilson_interval(successes: float, sample_size: int) -> tuple[float, float]:
    proportion = successes / sample_size
    z_squared = CALIBRATION_INTERVAL_Z**2
    denominator = 1.0 + z_squared / sample_size
    center = (proportion + z_squared / (2.0 * sample_size)) / denominator
    radius = (
        CALIBRATION_INTERVAL_Z
        * sqrt(proportion * (1.0 - proportion) / sample_size + z_squared / (4.0 * sample_size**2))
        / denominator
    )
    return max(0.0, center - radius), min(1.0, center + radius)


def _independence_error(
    predicted_totals: Sequence[float],
    observed_counts: Sequence[int],
    sample_size: int,
) -> IndependenceAssumptionError:
    if sample_size == 0:
        return IndependenceAssumptionError(None, None, ())
    outcomes = tuple(
        OutcomeFrequencyError(
            outcome=outcome,
            mean_predicted_probability=predicted_totals[index] / sample_size,
            observed_frequency=observed_counts[index] / sample_size,
            residual=(observed_counts[index] - predicted_totals[index]) / sample_size,
            sample_size=sample_size,
        )
        for index, outcome in enumerate(SET_OUTCOME_ORDER)
    )
    absolute = [abs(item.residual) for item in outcomes]
    return IndependenceAssumptionError(
        mean_absolute_frequency_error=sum(absolute) / len(absolute),
        maximum_absolute_frequency_error=max(absolute),
        outcomes=outcomes,
    )


def _result_outcome(match: EloMatch) -> SetOutcome | None:
    result = match.evaluation_result
    if result is None:
        return None
    return SetOutcome.from_result(result.home_sets, result.away_sets)


def _ordered_unique(matches: Sequence[EloMatch]) -> list[EloMatch]:
    if len({match.match_id for match in matches}) != len(matches):
        raise ValueError("match_id must be unique within an experiment")
    return sorted(matches, key=lambda match: match.order_key)


def _single_cohort(matches: Iterable[EloMatch]) -> CohortKey:
    cohorts = {CohortKey.from_match(match) for match in matches}
    if not cohorts:
        raise ValueError("an experiment requires matches")
    if len(cohorts) != 1:
        raise ValueError("experiment cohort dimensions must remain separate")
    return next(iter(cohorts))


def _partition(
    matches: Sequence[EloMatch],
    split: TemporalSplit,
) -> tuple[list[EloMatch], list[EloMatch], list[EloMatch]]:
    train: list[EloMatch] = []
    validation: list[EloMatch] = []
    test: list[EloMatch] = []
    for match in matches:
        cutoff = match.prediction_cutoff_at
        if split.train_start <= cutoff < split.train_end:
            train.append(match)
        elif split.validation_start <= cutoff < split.validation_end:
            validation.append(match)
        elif split.test_start <= cutoff < split.test_end:
            test.append(match)
    return train, validation, test
