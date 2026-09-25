"""Pure, versioned metrics for result-revision evaluation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite, log, sqrt
from typing import Any

LOG_LOSS_EPSILON = 1e-12
LOG_LOSS_VERSION = "binary-log-loss-epsilon-1e-12-v1"
BINARY_METRIC_VERSION = "binary-home-win-v1"
SET_RPS_VERSION = "set-rps-five-boundaries-v1"
SET_ACCURACY_VERSION = "fractional-tied-maxima-v1"
CALIBRATION_VERSION = "fixed-width-0.2-wilson-95-v1"
PAIRED_DIFFERENCE_VERSION = "paired-common-match-normal-95-v1"
MARKET_ACCURACY_VERSION = "wins-over-wins-plus-losses-v1"
CALIBRATION_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
WILSON_Z_95 = 1.959963984540054
SET_SCORE_HOME_ORDER = ("3:0", "3:1", "3:2", "2:3", "1:3", "0:3")
SET_SCORE_RPS_ORDER = tuple(reversed(SET_SCORE_HOME_ORDER))


@dataclass(frozen=True)
class CalibrationBin:
    lower_bound: float
    upper_bound: float
    includes_upper_bound: bool
    mean_probability: float
    observed_rate: float
    sample_size: int
    uncertainty_lower: float
    uncertainty_upper: float
    interval_version: str = CALIBRATION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "includes_upper_bound": self.includes_upper_bound,
            "mean_probability": self.mean_probability,
            "observed_rate": self.observed_rate,
            "sample_size": self.sample_size,
            "uncertainty_lower": self.uncertainty_lower,
            "uncertainty_upper": self.uncertainty_upper,
            "interval_version": self.interval_version,
        }


@dataclass(frozen=True)
class PairedDifference:
    metric: str
    sample_size: int
    mean_difference: float | None
    standard_error: float | None
    confidence_lower: float | None
    confidence_upper: float | None
    version: str = PAIRED_DIFFERENCE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "sample_size": self.sample_size,
            "mean_difference": self.mean_difference,
            "standard_error": self.standard_error,
            "confidence_lower": self.confidence_lower,
            "confidence_upper": self.confidence_upper,
            "version": self.version,
        }


@dataclass(frozen=True)
class MarketAccuracy:
    accuracy: float | None
    denominator: int
    counts: Mapping[str, int]
    version: str = MARKET_ACCURACY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "accuracy": self.accuracy,
            "denominator": self.denominator,
            "counts": dict(sorted(self.counts.items())),
            "version": self.version,
        }


def binary_brier(probability: float, outcome: int | bool) -> float:
    """Return the one-event binary Brier score using the original probability."""

    value = _probability(probability)
    target = _binary_outcome(outcome)
    return (value - target) ** 2


def binary_log_loss(
    probability: float,
    outcome: int | bool,
    *,
    epsilon: float = LOG_LOSS_EPSILON,
) -> float:
    """Return binary log loss, clipping only for the logarithm operation."""

    value = _probability(probability)
    target = _binary_outcome(outcome)
    if not isfinite(epsilon) or not 0 < epsilon < 0.5:
        raise ValueError("epsilon must be finite and between zero and 0.5")
    clipped = min(max(value, epsilon), 1.0 - epsilon)
    return -(target * log(clipped) + (1.0 - target) * log(1.0 - clipped))


def binary_accuracy(probability: float, outcome: int | bool) -> float:
    """Score a winner prediction with the fixed p >= 0.5 home tie rule."""

    value = _probability(probability)
    target = int(_binary_outcome(outcome))
    return float(int(value >= 0.5) == target)


def set_ranked_probability_score(
    probabilities: Mapping[str, float] | Sequence[float],
    actual_score: str,
) -> float:
    """Return RPS over the five weak-home to strong-home cumulative boundaries."""

    ordered = _set_probabilities(probabilities)
    if actual_score not in SET_SCORE_HOME_ORDER:
        raise ValueError("actual_score must be one of the six supported set scores")
    weak_to_strong = tuple(reversed(ordered))
    actual_index = SET_SCORE_RPS_ORDER.index(actual_score)
    predicted_cumulative = 0.0
    observed_cumulative = 0.0
    total = 0.0
    for index in range(len(weak_to_strong) - 1):
        predicted_cumulative += weak_to_strong[index]
        observed_cumulative += float(index == actual_index)
        total += (predicted_cumulative - observed_cumulative) ** 2
    return total / 5.0


def set_score_accuracy(
    probabilities: Mapping[str, float] | Sequence[float],
    actual_score: str,
) -> float:
    """Score an exact set result, sharing credit across tied maximum categories."""

    ordered = _set_probabilities(probabilities)
    if actual_score not in SET_SCORE_HOME_ORDER:
        raise ValueError("actual_score must be one of the six supported set scores")
    maximum = max(ordered)
    tied = tuple(index for index, probability in enumerate(ordered) if probability == maximum)
    actual_index = SET_SCORE_HOME_ORDER.index(actual_score)
    return 1.0 / len(tied) if actual_index in tied else 0.0


def calibration_bins(
    observations: Sequence[tuple[float, int | bool]],
) -> tuple[CalibrationBin, ...]:
    """Build fixed-width bins and Wilson uncertainty, omitting empty bins."""

    buckets: list[list[tuple[float, float]]] = [[] for _ in range(len(CALIBRATION_EDGES) - 1)]
    for raw_probability, raw_outcome in observations:
        probability = _probability(raw_probability)
        outcome = _binary_outcome(raw_outcome)
        index = min(int(probability * len(buckets)), len(buckets) - 1)
        buckets[index].append((probability, outcome))

    result: list[CalibrationBin] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        successes = sum(outcome for _, outcome in bucket)
        lower, upper = wilson_interval(successes, len(bucket))
        result.append(
            CalibrationBin(
                lower_bound=CALIBRATION_EDGES[index],
                upper_bound=CALIBRATION_EDGES[index + 1],
                includes_upper_bound=index == len(buckets) - 1,
                mean_probability=sum(probability for probability, _ in bucket) / len(bucket),
                observed_rate=successes / len(bucket),
                sample_size=len(bucket),
                uncertainty_lower=lower,
                uncertainty_upper=upper,
            )
        )
    return tuple(result)


def wilson_interval(successes: float, sample_size: int) -> tuple[float, float]:
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    if not isfinite(successes) or not 0 <= successes <= sample_size:
        raise ValueError("successes must be finite and within the sample")
    proportion = successes / sample_size
    z_squared = WILSON_Z_95**2
    denominator = 1.0 + z_squared / sample_size
    center = (proportion + z_squared / (2.0 * sample_size)) / denominator
    radius = (
        WILSON_Z_95
        * sqrt(proportion * (1.0 - proportion) / sample_size + z_squared / (4 * sample_size**2))
        / denominator
    )
    return max(0.0, center - radius), min(1.0, center + radius)


def paired_difference(values: Sequence[float], *, metric: str) -> PairedDifference:
    """Summarize per-match AI minus baseline losses on the common intersection."""

    if not metric.strip():
        raise ValueError("metric must not be blank")
    if any(not isfinite(value) for value in values):
        raise ValueError("paired differences must be finite")
    sample_size = len(values)
    if sample_size == 0:
        return PairedDifference(metric, 0, None, None, None, None)
    mean = sum(values) / sample_size
    if sample_size == 1:
        return PairedDifference(metric, 1, mean, None, None, None)
    variance = sum((value - mean) ** 2 for value in values) / (sample_size - 1)
    standard_error = sqrt(variance / sample_size)
    margin = WILSON_Z_95 * standard_error
    return PairedDifference(
        metric=metric,
        sample_size=sample_size,
        mean_difference=mean,
        standard_error=standard_error,
        confidence_lower=mean - margin,
        confidence_upper=mean + margin,
    )


def market_accuracy(outcomes: Sequence[str]) -> MarketAccuracy:
    """Use wins and losses only; preserve all exclusions as explicit counts."""

    allowed = {"win", "loss", "push", "void", "missing", "stale", "late", "unsupported"}
    normalized = [str(outcome).lower() for outcome in outcomes]
    invalid = sorted(set(normalized) - allowed)
    if invalid:
        raise ValueError(f"unsupported settlement outcomes: {', '.join(invalid)}")
    counts = Counter(normalized)
    for outcome in sorted(allowed):
        counts.setdefault(outcome, 0)
    denominator = counts["win"] + counts["loss"]
    return MarketAccuracy(
        accuracy=counts["win"] / denominator if denominator else None,
        denominator=denominator,
        counts=dict(counts),
    )


def _probability(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("probability must be numeric")
    result = float(value)
    if not isfinite(result) or not 0 <= result <= 1:
        raise ValueError("probability must be finite and between zero and one")
    return result


def _binary_outcome(value: int | bool) -> float:
    if value not in (0, 1, False, True):
        raise ValueError("outcome must be binary")
    return float(value)


def _set_probabilities(probabilities: Mapping[str, float] | Sequence[float]) -> tuple[float, ...]:
    if isinstance(probabilities, Mapping):
        if set(probabilities) != set(SET_SCORE_HOME_ORDER):
            raise ValueError("set probabilities must contain exactly the six supported scores")
        ordered = tuple(_probability(probabilities[key]) for key in SET_SCORE_HOME_ORDER)
    else:
        if len(probabilities) != len(SET_SCORE_HOME_ORDER):
            raise ValueError("set probabilities must contain exactly six values")
        ordered = tuple(_probability(value) for value in probabilities)
    if abs(sum(ordered) - 1.0) > 1e-6:
        raise ValueError("set probabilities must sum to one")
    return ordered
