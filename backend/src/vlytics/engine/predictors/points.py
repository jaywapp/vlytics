"""Versioned joint volleyball match-score distributions.

The model starts from the six-outcome set distribution and derives rally-level
score probabilities that obey the configured scoring rules. Exact convolution
is the default. A deterministic Monte Carlo implementation is retained for
larger future rule supports and records its numerical error separately from
the explicitly truncated deuce tail.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import comb, isfinite, sqrt
from types import MappingProxyType
from typing import Any

from vlytics.engine.predictors.elo import Division
from vlytics.engine.predictors.sets import (
    SET_OUTCOME_ORDER,
    PredictionCapability,
    SetOutcome,
    SetOutcomeDistribution,
    SetOutcomePrediction,
    SetOutcomeProbability,
)

DEFAULT_POINT_MODEL_VERSION = "conditional-rally-score-v1"
DEFAULT_JOINT_DISTRIBUTION_VERSION = "joint-score-v1"
DEFAULT_SCORE_RULE_VERSION = "kovo-rally-best-of-five-v1"
DEFAULT_POINT_RANDOM_SEED = 0
DEFAULT_MONTE_CARLO_SAMPLES = 100_000
DEFAULT_MAX_DEUCE_CYCLES = 24
JOINT_MASS_TOLERANCE = 1e-10
EXACT_SET_MARGINAL_TOLERANCE = 1e-12
DIAGNOSTIC_TOLERANCE = 1e-12
DEFAULT_MAX_MONTE_CARLO_SET_MARGINAL_ERROR = 0.03
SAMPLER_VERSION = "inverse-cdf-python-random-v1"


class DistributionMethod(StrEnum):
    EXACT = "exact"
    MONTE_CARLO = "monte_carlo"


@dataclass(frozen=True)
class ScoreRules:
    """One reviewed season scoring-rule version."""

    version: str
    sets_to_win: int = 3
    normal_target: int = 25
    deciding_target: int = 15
    win_by: int = 2

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("score rule version must not be blank")
        if self.sets_to_win != 3:
            raise ValueError("joint-score-v1 supports best-of-five matches")
        if self.normal_target < 2 or self.deciding_target < 2:
            raise ValueError("set targets must be at least two")
        if self.win_by != 2:
            raise ValueError("conditional-rally-score-v1 requires a two-point winning margin")

    def target_for_set(self, set_number: int) -> int:
        if not 1 <= set_number <= 5:
            raise ValueError("set_number must be between one and five")
        return self.deciding_target if set_number == 5 else self.normal_target

    def to_dict(self) -> dict[str, Any]:
        return {
            "deciding_target": self.deciding_target,
            "normal_target": self.normal_target,
            "sets_to_win": self.sets_to_win,
            "version": self.version,
            "win_by": self.win_by,
        }


SCORE_RULES: MappingProxyType[str, ScoreRules] = MappingProxyType(
    {DEFAULT_SCORE_RULE_VERSION: ScoreRules(DEFAULT_SCORE_RULE_VERSION)}
)


def score_rules_for_version(version: str) -> ScoreRules:
    """Resolve a season rule version without silently assuming unknown rules."""

    try:
        return SCORE_RULES[version]
    except KeyError as error:
        raise ValueError(f"unsupported score rule version: {version}") from error


@dataclass(frozen=True)
class VerifiedSeasonScoreRule:
    """Mirror-owned evidence that a season and division use one score rule."""

    season_id: str
    division: Division
    rule_version: str
    mirror_rule_artifact_id: str
    verified_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "division", Division(self.division))
        if not self.season_id.strip():
            raise ValueError("verified score rule season_id must not be blank")
        score_rules_for_version(self.rule_version)
        _require_sha256(self.mirror_rule_artifact_id, "mirror_rule_artifact_id")
        _require_aware(self.verified_at, "verified_at")

    @property
    def rules(self) -> ScoreRules:
        return score_rules_for_version(self.rule_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "division": self.division.value,
            "mirror_rule_artifact_id": self.mirror_rule_artifact_id,
            "rule_version": self.rule_version,
            "season_id": self.season_id,
            "verified_at": self.verified_at.isoformat(),
        }


@dataclass(frozen=True, order=True)
class SetPointScore:
    home_points: int
    away_points: int

    def __post_init__(self) -> None:
        for field_name in ("home_points", "away_points"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("set points must be non-negative integers")
        if self.home_points == self.away_points:
            raise ValueError("a completed set cannot be tied")

    @property
    def home_won(self) -> bool:
        return self.home_points > self.away_points

    def to_dict(self) -> dict[str, int]:
        return {"away_points": self.away_points, "home_points": self.home_points}


def is_valid_set_score(score: SetPointScore, target: int, win_by: int = 2) -> bool:
    """Return whether a completed set satisfies target and win-by rules."""

    winner = max(score.home_points, score.away_points)
    loser = min(score.home_points, score.away_points)
    if winner < target or winner - loser < win_by:
        return False
    if winner == target:
        return loser <= target - win_by
    return winner - loser == win_by and loser >= target - 1


@dataclass(frozen=True)
class SetScoreProbability:
    score: SetPointScore
    probability: float

    def __post_init__(self) -> None:
        probability = float(self.probability)
        object.__setattr__(self, "probability", probability)
        if not isfinite(probability) or probability < 0:
            raise ValueError("set score probability must be finite and non-negative")


@dataclass(frozen=True)
class ConditionalSetScoreDistribution:
    """A finite conditional score PMF plus its omitted infinite deuce tail."""

    target: int
    home_won: bool
    home_set_win_probability: float
    home_rally_probability: float
    outcomes: tuple[SetScoreProbability, ...]
    conditional_tail_probability: float
    max_deuce_cycles: int

    def __post_init__(self) -> None:
        if not self.outcomes:
            raise ValueError("conditional set score distribution cannot be empty")
        mass = sum(item.probability for item in self.outcomes)
        if abs(mass - 1.0) > JOINT_MASS_TOLERANCE:
            raise ValueError("conditional set score mass must sum to one")
        for item in self.outcomes:
            if item.score.home_won is not self.home_won:
                raise ValueError("conditional score winner does not match its distribution")
            if not is_valid_set_score(item.score, self.target):
                raise ValueError("conditional distribution contains an invalid set score")
        if not 0 <= self.conditional_tail_probability <= 1:
            raise ValueError("conditional tail probability must be between zero and one")

    def as_mapping(self) -> MappingProxyType[SetPointScore, float]:
        return MappingProxyType({item.score: item.probability for item in self.outcomes})


@dataclass(frozen=True)
class PointModelConfig:
    """Immutable rule, truncation, and deterministic generation settings."""

    score_rule_version: str
    model_version: str = DEFAULT_POINT_MODEL_VERSION
    distribution_version: str = DEFAULT_JOINT_DISTRIBUTION_VERSION
    method: DistributionMethod = DistributionMethod.EXACT
    random_seed: int = DEFAULT_POINT_RANDOM_SEED
    sample_count: int = DEFAULT_MONTE_CARLO_SAMPLES
    max_deuce_cycles: int = DEFAULT_MAX_DEUCE_CYCLES
    max_monte_carlo_set_marginal_error: float = DEFAULT_MAX_MONTE_CARLO_SET_MARGINAL_ERROR

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", DistributionMethod(self.method))
        if not self.model_version.strip() or not self.distribution_version.strip():
            raise ValueError("model and distribution versions must not be blank")
        score_rules_for_version(self.score_rule_version)
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise ValueError("random_seed must be an integer")
        if not isinstance(self.sample_count, int) or isinstance(self.sample_count, bool):
            raise ValueError("sample_count must be an integer")
        if self.sample_count <= 0:
            raise ValueError("sample_count must be positive")
        if not isinstance(self.max_deuce_cycles, int) or isinstance(self.max_deuce_cycles, bool):
            raise ValueError("max_deuce_cycles must be an integer")
        if self.max_deuce_cycles < 0:
            raise ValueError("max_deuce_cycles must be non-negative")
        threshold = float(self.max_monte_carlo_set_marginal_error)
        object.__setattr__(self, "max_monte_carlo_set_marginal_error", threshold)
        if not isfinite(threshold) or not 0 < threshold <= 1:
            raise ValueError("max_monte_carlo_set_marginal_error must be finite and in (0, 1]")

    @property
    def capabilities(self) -> tuple[PredictionCapability, ...]:
        return (
            PredictionCapability.WINNER,
            PredictionCapability.SET_SCORE,
            PredictionCapability.POINT_HANDICAP,
            PredictionCapability.POINT_TOTAL,
        )

    @property
    def config_id(self) -> str:
        return _canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "capabilities": [item.value for item in self.capabilities],
            "distribution_version": self.distribution_version,
            "max_deuce_cycles": self.max_deuce_cycles,
            "max_monte_carlo_set_marginal_error": (self.max_monte_carlo_set_marginal_error),
            "method": self.method.value,
            "model_version": self.model_version,
            "random_seed": self.random_seed,
            "sample_count": self.sample_count,
            "sampler_version": SAMPLER_VERSION,
            "score_rule_version": self.score_rule_version,
        }


@dataclass(frozen=True)
class VerifiedSetPredictionArtifact:
    """Persistence-owned identity attached to a validated set prediction."""

    prediction: SetOutcomePrediction
    prediction_id: str
    producer_variant_id: str
    input_snapshot_id: str

    def __post_init__(self) -> None:
        if not self.producer_variant_id.strip() or not self.input_snapshot_id.strip():
            raise ValueError("set prediction variant and snapshot IDs must not be blank")
        expected = self.artifact_id(
            self.prediction,
            producer_variant_id=self.producer_variant_id,
            input_snapshot_id=self.input_snapshot_id,
        )
        if self.prediction_id != expected:
            raise ValueError("set prediction ID does not match its canonical artifact")

    @classmethod
    def capture(
        cls,
        prediction: SetOutcomePrediction,
        *,
        producer_variant_id: str,
        input_snapshot_id: str,
    ) -> VerifiedSetPredictionArtifact:
        prediction_id = cls.artifact_id(
            prediction,
            producer_variant_id=producer_variant_id,
            input_snapshot_id=input_snapshot_id,
        )
        return cls(
            prediction=prediction,
            prediction_id=prediction_id,
            producer_variant_id=producer_variant_id,
            input_snapshot_id=input_snapshot_id,
        )

    @staticmethod
    def artifact_id(
        prediction: SetOutcomePrediction,
        *,
        producer_variant_id: str,
        input_snapshot_id: str,
    ) -> str:
        if not producer_variant_id.strip() or not input_snapshot_id.strip():
            raise ValueError("set prediction variant and snapshot IDs must not be blank")
        return _canonical_sha256(
            {
                "input_snapshot_id": input_snapshot_id,
                "prediction": prediction.to_dict(),
                "producer_variant_id": producer_variant_id,
            }
        )

    def to_lineage_dict(self) -> dict[str, Any]:
        prediction = self.prediction
        return {
            "config_id": prediction.config_id,
            "distribution_version": prediction.distribution_version,
            "fitted_parameter_artifact_id": prediction.fitted_parameter_artifact_id,
            "input_snapshot_id": self.input_snapshot_id,
            "match_id": prediction.match_id,
            "model_version": prediction.model_version,
            "prediction_cutoff_at": prediction.prediction_cutoff_at.isoformat(),
            "prediction_id": self.prediction_id,
            "producer_variant_id": self.producer_variant_id,
            "random_seed": prediction.random_seed,
            "schedule_revision_id": prediction.schedule_revision_id,
            "training_as_of": prediction.training_as_of.isoformat(),
            "training_cohort": prediction.training_cohort.to_dict(),
            "training_result_manifest_sha256": prediction.training_result_manifest_sha256,
            "upstream_elo": {
                "config_id": prediction.source_config_id,
                "model_version": prediction.source_model_version,
                "prediction_id": prediction.source_prediction_id,
            },
        }


@dataclass(frozen=True)
class JointScoreAtom:
    set_score: SetOutcome
    home_points: int
    away_points: int
    probability: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "set_score", SetOutcome(self.set_score))
        for field_name in ("home_points", "away_points"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("match points must be non-negative integers")
        probability = float(self.probability)
        object.__setattr__(self, "probability", probability)
        if not isfinite(probability) or probability <= 0:
            raise ValueError("joint score probability must be finite and positive")

    @property
    def total_points(self) -> int:
        return self.home_points + self.away_points

    @property
    def point_differential(self) -> int:
        return self.home_points - self.away_points

    def to_dict(self) -> dict[str, Any]:
        return {
            "away_points": self.away_points,
            "home_points": self.home_points,
            "probability": self.probability,
            "set_score": self.set_score.value,
        }


@dataclass(frozen=True)
class DistributionDiagnostics:
    total_mass_error: float
    source_set_marginal_max_abs_error: float
    deuce_truncation_error_upper_bound: float
    maximum_conditional_set_tail_probability: float
    monte_carlo_max_standard_error_95: float

    def __post_init__(self) -> None:
        for field_name in (
            "total_mass_error",
            "source_set_marginal_max_abs_error",
            "deuce_truncation_error_upper_bound",
            "maximum_conditional_set_tail_probability",
            "monte_carlo_max_standard_error_95",
        ):
            value = float(getattr(self, field_name))
            object.__setattr__(self, field_name, value)
            if not isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{field_name} must be finite and between zero and one")

    def to_dict(self) -> dict[str, float]:
        return {
            "deuce_truncation_error_upper_bound": self.deuce_truncation_error_upper_bound,
            "maximum_conditional_set_tail_probability": (
                self.maximum_conditional_set_tail_probability
            ),
            "monte_carlo_max_standard_error_95": self.monte_carlo_max_standard_error_95,
            "source_set_marginal_max_abs_error": self.source_set_marginal_max_abs_error,
            "total_mass_error": self.total_mass_error,
        }


@dataclass(frozen=True)
class LineProbabilities:
    above: float
    push: float
    below: float

    def __post_init__(self) -> None:
        values = (float(self.above), float(self.push), float(self.below))
        object.__setattr__(self, "above", values[0])
        object.__setattr__(self, "push", values[1])
        object.__setattr__(self, "below", values[2])
        if any(not isfinite(value) or value < 0 for value in values):
            raise ValueError("line probabilities must be finite and non-negative")
        if abs(sum(values) - 1.0) > JOINT_MASS_TOLERANCE:
            raise ValueError("line probabilities must sum to one")

    def to_dict(self) -> dict[str, float]:
        return {"above": self.above, "below": self.below, "push": self.push}


@dataclass(frozen=True)
class JointScoreDistribution:
    """Finite P(set score, home points, away points) with recorded errors."""

    entries: tuple[JointScoreAtom, ...]
    source_set_distribution: SetOutcomeDistribution
    division: Division
    source_set_artifact: VerifiedSetPredictionArtifact
    producer_variant_id: str
    input_snapshot_id: str
    model_version: str
    distribution_version: str
    config_id: str
    score_rule_identity: VerifiedSeasonScoreRule
    score_rules: ScoreRules
    method: DistributionMethod
    random_seed: int
    sample_count: int
    configured_sample_count: int
    max_deuce_cycles: int
    max_monte_carlo_set_marginal_error: float
    diagnostics: DistributionDiagnostics

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", DistributionMethod(self.method))
        object.__setattr__(self, "division", Division(self.division))
        source_prediction = self.source_set_artifact.prediction
        if self.source_set_distribution != source_prediction.distribution:
            raise ValueError("source set distribution must match its prediction artifact")
        if self.division is not source_prediction.division:
            raise ValueError("joint distribution division must match its source set prediction")
        if not self.producer_variant_id.strip():
            raise ValueError("joint producer_variant_id must not be blank")
        if self.input_snapshot_id != self.source_set_artifact.input_snapshot_id:
            raise ValueError("joint and source set artifacts must use the same input snapshot")
        if self.score_rule_identity.division is not self.division:
            raise ValueError("verified score rule division must match the prediction")
        if self.score_rule_identity.rule_version != self.score_rules.version:
            raise ValueError("verified score rule identity and scoring rules must agree")
        expected_config = PointModelConfig(
            score_rule_version=self.score_rules.version,
            model_version=self.model_version,
            distribution_version=self.distribution_version,
            method=self.method,
            random_seed=self.random_seed,
            sample_count=self.configured_sample_count,
            max_deuce_cycles=self.max_deuce_cycles,
            max_monte_carlo_set_marginal_error=(self.max_monte_carlo_set_marginal_error),
        )
        if self.config_id != expected_config.config_id:
            raise ValueError("joint distribution config_id is inconsistent with its settings")
        if not self.entries:
            raise ValueError("joint score distribution cannot be empty")
        keys = {(item.set_score, item.home_points, item.away_points) for item in self.entries}
        if len(keys) != len(self.entries):
            raise ValueError("joint score atoms must be unique")
        mass_error = abs(sum(item.probability for item in self.entries) - 1.0)
        if mass_error > JOINT_MASS_TOLERANCE:
            raise ValueError("joint score probability mass must sum to one")
        feasible = {
            outcome: _feasible_match_support(
                outcome,
                self.score_rules,
                self.max_deuce_cycles,
            )
            for outcome in {entry.set_score for entry in self.entries}
        }
        for entry in self.entries:
            if (entry.home_points, entry.away_points) not in feasible[entry.set_score]:
                raise ValueError("joint distribution contains an impossible match score")
        derived_set = _set_marginal(self.entries)
        marginal_error = max(
            abs(derived_set[outcome] - self.source_set_distribution[outcome])
            for outcome in SET_OUTCOME_ORDER
        )
        if abs(self.diagnostics.total_mass_error - mass_error) > DIAGNOSTIC_TOLERANCE:
            raise ValueError("recorded total mass error is inconsistent")
        if (
            abs(self.diagnostics.source_set_marginal_max_abs_error - marginal_error)
            > DIAGNOSTIC_TOLERANCE
        ):
            raise ValueError("recorded source set marginal error is inconsistent")
        conditional = _conditional_distributions(
            self.source_set_distribution,
            _validate_probability(
                source_prediction.regular_set_home_win_probability,
                "regular_set_home_win_probability",
            ),
            _validate_probability(
                source_prediction.fifth_set_home_win_probability,
                "deciding_set_home_win_probability",
            ),
            self.score_rules,
            self.max_deuce_cycles,
        )
        expected_tail_error, expected_maximum_tail = _match_tail_bound(
            self.source_set_distribution,
            conditional,
        )
        if (
            abs(self.diagnostics.deuce_truncation_error_upper_bound - expected_tail_error)
            > DIAGNOSTIC_TOLERANCE
        ):
            raise ValueError("recorded deuce truncation error is inconsistent")
        if (
            abs(self.diagnostics.maximum_conditional_set_tail_probability - expected_maximum_tail)
            > DIAGNOSTIC_TOLERANCE
        ):
            raise ValueError("recorded maximum conditional set tail is inconsistent")
        if self.method is DistributionMethod.EXACT:
            if self.sample_count != 0:
                raise ValueError("exact distributions must record zero samples")
            if marginal_error > EXACT_SET_MARGINAL_TOLERANCE:
                raise ValueError("exact joint distribution does not preserve the set marginal")
            expected_sampling_error = 0.0
        else:
            if self.sample_count != self.configured_sample_count:
                raise ValueError("Monte Carlo sample count is inconsistent with its config")
            if marginal_error > self.max_monte_carlo_set_marginal_error:
                raise ValueError("Monte Carlo set marginal error exceeds its quality threshold")
            expected_sampling_error = 0.98 / sqrt(self.sample_count)
        if (
            abs(self.diagnostics.monte_carlo_max_standard_error_95 - expected_sampling_error)
            > DIAGNOSTIC_TOLERANCE
        ):
            raise ValueError("recorded Monte Carlo error is inconsistent")

    @property
    def distribution_id(self) -> str:
        return _canonical_sha256(self.to_artifact(include_distribution_id=False))

    @property
    def home_win_probability(self) -> float:
        return sum(
            entry.probability for entry in self.entries if entry.set_score in SET_OUTCOME_ORDER[:3]
        )

    @property
    def set_score_distribution(self) -> SetOutcomeDistribution:
        totals: defaultdict[SetOutcome, float] = defaultdict(float)
        for entry in self.entries:
            totals[entry.set_score] += entry.probability
        return SetOutcomeDistribution(
            tuple(SetOutcomeProbability(outcome, totals[outcome]) for outcome in SET_OUTCOME_ORDER)
        )

    @property
    def point_total_distribution(self) -> MappingProxyType[int, float]:
        totals: defaultdict[int, float] = defaultdict(float)
        for entry in self.entries:
            totals[entry.total_points] += entry.probability
        return MappingProxyType(dict(sorted(totals.items())))

    @property
    def point_differential_distribution(self) -> MappingProxyType[int, float]:
        totals: defaultdict[int, float] = defaultdict(float)
        for entry in self.entries:
            totals[entry.point_differential] += entry.probability
        return MappingProxyType(dict(sorted(totals.items())))

    def exact_probability(
        self,
        set_score: SetOutcome | str,
        home_points: int,
        away_points: int,
    ) -> float:
        expected = SetOutcome(set_score)
        return sum(
            entry.probability
            for entry in self.entries
            if entry.set_score is expected
            and entry.home_points == home_points
            and entry.away_points == away_points
        )

    def point_total(self, line: float) -> LineProbabilities:
        return _line_probabilities(self.point_total_distribution, line)

    def point_handicap(self, home_handicap: float) -> LineProbabilities:
        """Return home cover/push/fail as above/push/below."""

        differential = self.point_differential_distribution
        shifted = {
            value + home_handicap: probability for value, probability in differential.items()
        }
        return _line_probabilities(shifted, 0.0)

    def to_artifact(self, *, include_distribution_id: bool = True) -> dict[str, Any]:
        artifact: dict[str, Any] = {
            "schema_version": "joint-score-v1",
            "model_version": self.model_version,
            "distribution_version": self.distribution_version,
            "config_id": self.config_id,
            "division": self.division.value,
            "ownership": {
                "input_snapshot_id": self.input_snapshot_id,
                "producer_variant_id": self.producer_variant_id,
            },
            "source_set_prediction": self.source_set_artifact.to_lineage_dict(),
            "verified_score_rule": self.score_rule_identity.to_dict(),
            "score_rules": self.score_rules.to_dict(),
            "generation": {
                "configured_sample_count": self.configured_sample_count,
                "max_deuce_cycles": self.max_deuce_cycles,
                "max_monte_carlo_set_marginal_error": (self.max_monte_carlo_set_marginal_error),
                "method": self.method.value,
                "random_seed": self.random_seed,
                "sample_count": self.sample_count,
                "sampler_version": SAMPLER_VERSION,
            },
            "diagnostics": self.diagnostics.to_dict(),
            "source_set_score_probabilities": self.source_set_distribution.to_list(),
            "entries": [entry.to_dict() for entry in self.entries],
            "marginals": {
                "home_win_probability": self.home_win_probability,
                "point_differentials": [
                    {"point_differential": value, "probability": probability}
                    for value, probability in self.point_differential_distribution.items()
                ],
                "point_totals": [
                    {"probability": probability, "total_points": value}
                    for value, probability in self.point_total_distribution.items()
                ],
                "set_score_probabilities": self.set_score_distribution.to_list(),
            },
        }
        if include_distribution_id:
            artifact["distribution_id"] = self.distribution_id
        return artifact


@dataclass(frozen=True)
class JointScorePrediction:
    match_id: str
    division: Division
    source_set_artifact: VerifiedSetPredictionArtifact
    distribution: JointScoreDistribution
    config: PointModelConfig

    def __post_init__(self) -> None:
        source_prediction = self.source_set_artifact.prediction
        if self.match_id != source_prediction.match_id:
            raise ValueError("point and set prediction match IDs must agree")
        if self.division is not source_prediction.division:
            raise ValueError("point and set prediction divisions must agree")
        if self.distribution.division is not self.division:
            raise ValueError("joint distribution division must match its prediction")
        if self.distribution.source_set_artifact != self.source_set_artifact:
            raise ValueError("joint distribution must preserve its source set artifact")
        if self.distribution.config_id != self.config.config_id:
            raise ValueError("joint prediction config must match its distribution")

    @property
    def capabilities(self) -> tuple[PredictionCapability, ...]:
        return self.config.capabilities

    def to_contract(
        self,
        *,
        producer_variant_id: str | None = None,
        input_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        stored_variant = self.distribution.producer_variant_id
        stored_snapshot = self.distribution.input_snapshot_id
        if producer_variant_id is not None and producer_variant_id != stored_variant:
            raise ValueError("producer_variant_id does not match the stored joint artifact")
        if input_snapshot_id is not None and input_snapshot_id != stored_snapshot:
            raise ValueError("input_snapshot_id does not match the stored joint artifact")
        source_prediction = self.source_set_artifact.prediction
        provenance = {
            "producer_variant_id": stored_variant,
            "distribution_version": self.distribution.distribution_version,
            "probability_source": "statistical_derived",
            "derivation_method": "joint_score_distribution_marginalization",
            "input_snapshot_id": stored_snapshot,
            "source_set_config_id": source_prediction.config_id,
            "source_set_model_version": source_prediction.model_version,
            "source_set_prediction_id": self.source_set_artifact.prediction_id,
            "source_set_producer_variant_id": (self.source_set_artifact.producer_variant_id),
            **source_prediction.statistical_lineage(),
        }
        return {
            "schema_version": "prediction-v1",
            "producer_variant_id": stored_variant,
            "model_version": self.distribution.model_version,
            "distribution_version": self.distribution.distribution_version,
            "config_id": self.config.config_id,
            "random_seed": self.config.random_seed,
            "division": self.division.value,
            "capabilities": [item.value for item in self.capabilities],
            "home_win_probability": self.distribution.home_win_probability,
            "set_score_probabilities": self.distribution.set_score_distribution.to_list(),
            "joint_score_distribution_ref": self.distribution.distribution_id,
            "target_provenance": {
                capability.value: dict(provenance) for capability in self.capabilities
            },
            "rationale": (
                "All four targets are marginalized from one versioned joint distribution "
                "of final set score and both teams' total points."
            ),
            "risk_factors": [
                "constant_rally_probability_within_regular_and_deciding_sets",
                "finite_deuce_tail_is_renormalized_and_error_is_recorded",
            ],
        }


class JointScorePredictor:
    def __init__(self, config: PointModelConfig, *, producer_variant_id: str) -> None:
        if not producer_variant_id.strip():
            raise ValueError("producer_variant_id must not be blank")
        self.config = config
        self.producer_variant_id = producer_variant_id
        self.score_rules = score_rules_for_version(self.config.score_rule_version)

    def predict(
        self,
        source_set_artifact: VerifiedSetPredictionArtifact,
        score_rule_identity: VerifiedSeasonScoreRule,
    ) -> JointScorePrediction:
        set_prediction = source_set_artifact.prediction
        if score_rule_identity.division is not set_prediction.division:
            raise ValueError("verified score rule division must match the prediction")
        if score_rule_identity.rule_version != self.config.score_rule_version:
            raise ValueError("verified score rule does not match the point model config")
        distribution = build_joint_score_distribution(
            source_set_artifact,
            score_rule_identity,
            producer_variant_id=self.producer_variant_id,
            config=self.config,
        )
        return JointScorePrediction(
            match_id=set_prediction.match_id,
            division=set_prediction.division,
            source_set_artifact=source_set_artifact,
            distribution=distribution,
            config=self.config,
        )


def build_joint_score_distribution(
    source_set_artifact: VerifiedSetPredictionArtifact,
    score_rule_identity: VerifiedSeasonScoreRule,
    *,
    producer_variant_id: str,
    config: PointModelConfig,
) -> JointScoreDistribution:
    """Build one joint PMF using exact convolution or deterministic sampling."""

    selected_config = config
    set_prediction = source_set_artifact.prediction
    set_distribution = set_prediction.distribution
    rules = score_rules_for_version(selected_config.score_rule_version)
    if score_rule_identity.rule_version != rules.version:
        raise ValueError("verified season score rule does not match the model config")
    if score_rule_identity.division is not set_prediction.division:
        raise ValueError("verified season score rule division does not match the prediction")
    regular_probability = _validate_probability(
        set_prediction.regular_set_home_win_probability,
        "regular_set_home_win_probability",
    )
    deciding_probability = _validate_probability(
        set_prediction.fifth_set_home_win_probability,
        "deciding_set_home_win_probability",
    )
    conditional = _conditional_distributions(
        set_distribution,
        regular_probability,
        deciding_probability,
        rules,
        selected_config.max_deuce_cycles,
    )
    tail_error, maximum_tail = _match_tail_bound(set_distribution, conditional)
    if selected_config.method is DistributionMethod.EXACT:
        probabilities = _exact_joint_probabilities(set_distribution, conditional)
        actual_sample_count = 0
        monte_carlo_error = 0.0
    else:
        probabilities = _monte_carlo_joint_probabilities(
            set_distribution,
            conditional,
            selected_config.random_seed,
            selected_config.sample_count,
        )
        actual_sample_count = selected_config.sample_count
        monte_carlo_error = 0.98 / sqrt(selected_config.sample_count)
    entries = tuple(
        JointScoreAtom(outcome, home, away, probability)
        for (outcome, home, away), probability in sorted(
            probabilities.items(),
            key=lambda item: (
                SET_OUTCOME_ORDER.index(item[0][0]),
                item[0][1],
                item[0][2],
            ),
        )
        if probability > 0
    )
    mass_error = abs(sum(item.probability for item in entries) - 1.0)
    derived_set = _set_marginal(entries)
    marginal_error = max(
        abs(derived_set[outcome] - set_distribution[outcome]) for outcome in SET_OUTCOME_ORDER
    )
    diagnostics = DistributionDiagnostics(
        total_mass_error=mass_error,
        source_set_marginal_max_abs_error=marginal_error,
        deuce_truncation_error_upper_bound=tail_error,
        maximum_conditional_set_tail_probability=maximum_tail,
        monte_carlo_max_standard_error_95=monte_carlo_error,
    )
    return JointScoreDistribution(
        entries=entries,
        source_set_distribution=set_distribution,
        division=set_prediction.division,
        source_set_artifact=source_set_artifact,
        producer_variant_id=producer_variant_id,
        input_snapshot_id=source_set_artifact.input_snapshot_id,
        model_version=selected_config.model_version,
        distribution_version=selected_config.distribution_version,
        config_id=selected_config.config_id,
        score_rule_identity=score_rule_identity,
        score_rules=rules,
        method=selected_config.method,
        random_seed=selected_config.random_seed,
        sample_count=actual_sample_count,
        configured_sample_count=selected_config.sample_count,
        max_deuce_cycles=selected_config.max_deuce_cycles,
        max_monte_carlo_set_marginal_error=(selected_config.max_monte_carlo_set_marginal_error),
        diagnostics=diagnostics,
    )


def conditional_set_score_distribution(
    home_set_win_probability: float,
    *,
    home_won: bool,
    target: int,
    max_deuce_cycles: int = DEFAULT_MAX_DEUCE_CYCLES,
) -> ConditionalSetScoreDistribution:
    """Create P(final set points | winner) from an i.i.d. rally model."""

    set_probability = _validate_probability(
        home_set_win_probability,
        "home_set_win_probability",
    )
    if target < 2:
        raise ValueError("target must be at least two")
    if max_deuce_cycles < 0:
        raise ValueError("max_deuce_cycles must be non-negative")
    winner_probability = set_probability if home_won else 1.0 - set_probability
    if winner_probability == 0:
        raise ValueError("cannot condition on a zero-probability set winner")
    rally_probability = solve_rally_probability(set_probability, target)
    q = 1.0 - rally_probability
    raw: list[tuple[SetPointScore, float]] = []
    for loser_points in range(target - 1):
        if home_won:
            score = SetPointScore(target, loser_points)
            probability = (
                comb(target + loser_points - 1, loser_points)
                * rally_probability**target
                * q**loser_points
            )
        else:
            score = SetPointScore(loser_points, target)
            probability = (
                comb(target + loser_points - 1, loser_points)
                * q**target
                * rally_probability**loser_points
            )
        raw.append((score, probability / winner_probability))

    deuce_probability = (
        comb(2 * target - 2, target - 1) * rally_probability ** (target - 1) * q ** (target - 1)
    )
    continuation = 2.0 * rally_probability * q
    winner_pair_probability = rally_probability**2 if home_won else q**2
    for cycle in range(max_deuce_cycles + 1):
        loser_points = target - 1 + cycle
        winner_points = loser_points + 2
        score = (
            SetPointScore(winner_points, loser_points)
            if home_won
            else SetPointScore(loser_points, winner_points)
        )
        probability = (
            deuce_probability * continuation**cycle * winner_pair_probability
        ) / winner_probability
        raw.append((score, probability))

    pair_resolution_probability = rally_probability**2 + q**2
    winner_tail = 0.0
    if pair_resolution_probability > 0:
        winner_tail = (
            deuce_probability
            * continuation ** (max_deuce_cycles + 1)
            * winner_pair_probability
            / pair_resolution_probability
            / winner_probability
        )
    included_mass = sum(probability for _, probability in raw)
    if included_mass <= 0:
        raise ValueError("conditional set score support has no probability mass")
    outcomes = tuple(
        SetScoreProbability(score, probability / included_mass)
        for score, probability in raw
        if probability > 0
    )
    return ConditionalSetScoreDistribution(
        target=target,
        home_won=home_won,
        home_set_win_probability=set_probability,
        home_rally_probability=rally_probability,
        outcomes=outcomes,
        conditional_tail_probability=winner_tail,
        max_deuce_cycles=max_deuce_cycles,
    )


def set_win_probability_from_rally(home_rally_probability: float, target: int) -> float:
    """Analytic home set-win probability, including the infinite deuce tail."""

    rally = _validate_probability(home_rally_probability, "home_rally_probability")
    if target < 2:
        raise ValueError("target must be at least two")
    q = 1.0 - rally
    before_deuce = sum(
        comb(target + loser_points - 1, loser_points) * rally**target * q**loser_points
        for loser_points in range(target - 1)
    )
    deuce_probability = comb(2 * target - 2, target - 1) * rally ** (target - 1) * q ** (target - 1)
    pair_resolution_probability = rally**2 + q**2
    deuce_home_win = rally**2 / pair_resolution_probability if pair_resolution_probability else 0.5
    return before_deuce + deuce_probability * deuce_home_win


def solve_rally_probability(home_set_win_probability: float, target: int) -> float:
    """Invert the monotone rally-to-set probability relationship."""

    target_probability = _validate_probability(
        home_set_win_probability,
        "home_set_win_probability",
    )
    if target_probability in {0.0, 1.0}:
        return target_probability
    low = 0.0
    high = 1.0
    for _ in range(80):
        midpoint = (low + high) / 2.0
        if set_win_probability_from_rally(midpoint, target) < target_probability:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2.0


def is_feasible_match_total(
    outcome: SetOutcome | str,
    home_points: int,
    away_points: int,
    rules: ScoreRules | None = None,
    max_deuce_cycles: int = DEFAULT_MAX_DEUCE_CYCLES,
) -> bool:
    """Check aggregate reachability under the finite configured score support."""

    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (home_points, away_points)
    ):
        return False
    selected_rules = rules or score_rules_for_version(DEFAULT_SCORE_RULE_VERSION)
    expected = SetOutcome(outcome)
    return (home_points, away_points) in _feasible_match_support(
        expected,
        selected_rules,
        max_deuce_cycles,
    )


def _feasible_match_support(
    outcome: SetOutcome,
    rules: ScoreRules,
    max_deuce_cycles: int,
) -> set[tuple[int, int]]:
    home_sets, away_sets = _outcome_set_counts(outcome)
    regular_home, regular_away, deciding_home = _set_type_counts(home_sets, away_sets)
    support: set[tuple[int, int]] = {(0, 0)}
    unconditioned_regular = {
        winner: _valid_score_support(
            rules.normal_target,
            winner,
            max_deuce_cycles,
        )
        for winner in (True, False)
    }
    for home_won, count in ((True, regular_home), (False, regular_away)):
        for _ in range(count):
            support = _convolve_support(support, unconditioned_regular[home_won])
    if deciding_home is not None:
        support = _convolve_support(
            support,
            _valid_score_support(
                rules.deciding_target,
                deciding_home,
                max_deuce_cycles,
            ),
        )
    return support


def _conditional_distributions(
    set_distribution: SetOutcomeDistribution,
    regular_probability: float,
    deciding_probability: float,
    rules: ScoreRules,
    max_deuce_cycles: int,
) -> dict[tuple[str, bool], ConditionalSetScoreDistribution]:
    needed_regular: set[bool] = set()
    needed_deciding: set[bool] = set()
    for outcome in SET_OUTCOME_ORDER:
        if set_distribution[outcome] == 0:
            continue
        home_sets, away_sets = _outcome_set_counts(outcome)
        regular_home, regular_away, deciding_home = _set_type_counts(home_sets, away_sets)
        if regular_home:
            needed_regular.add(True)
        if regular_away:
            needed_regular.add(False)
        if deciding_home is not None:
            needed_deciding.add(deciding_home)
    conditional: dict[tuple[str, bool], ConditionalSetScoreDistribution] = {}
    for home_won in needed_regular:
        conditional[("regular", home_won)] = conditional_set_score_distribution(
            regular_probability,
            home_won=home_won,
            target=rules.normal_target,
            max_deuce_cycles=max_deuce_cycles,
        )
    for home_won in needed_deciding:
        conditional[("deciding", home_won)] = conditional_set_score_distribution(
            deciding_probability,
            home_won=home_won,
            target=rules.deciding_target,
            max_deuce_cycles=max_deuce_cycles,
        )
    return conditional


def _exact_joint_probabilities(
    set_distribution: SetOutcomeDistribution,
    conditional: dict[tuple[str, bool], ConditionalSetScoreDistribution],
) -> dict[tuple[SetOutcome, int, int], float]:
    joint: defaultdict[tuple[SetOutcome, int, int], float] = defaultdict(float)
    for outcome in SET_OUTCOME_ORDER:
        outcome_probability = set_distribution[outcome]
        if outcome_probability == 0:
            continue
        aggregate: dict[tuple[int, int], float] = {(0, 0): 1.0}
        home_sets, away_sets = _outcome_set_counts(outcome)
        regular_home, regular_away, deciding_home = _set_type_counts(home_sets, away_sets)
        for key, count in (
            (("regular", True), regular_home),
            (("regular", False), regular_away),
        ):
            for _ in range(count):
                aggregate = _convolve_probabilities(aggregate, conditional[key])
        if deciding_home is not None:
            aggregate = _convolve_probabilities(
                aggregate,
                conditional[("deciding", deciding_home)],
            )
        for (home_points, away_points), probability in aggregate.items():
            joint[(outcome, home_points, away_points)] += outcome_probability * probability
    return dict(joint)


def _monte_carlo_joint_probabilities(
    set_distribution: SetOutcomeDistribution,
    conditional: dict[tuple[str, bool], ConditionalSetScoreDistribution],
    seed: int,
    sample_count: int,
) -> dict[tuple[SetOutcome, int, int], float]:
    rng = random.Random(seed)
    outcome_probabilities = set_distribution.probabilities
    counts: defaultdict[tuple[SetOutcome, int, int], int] = defaultdict(int)
    score_tables = {
        key: (
            tuple(item.score for item in distribution.outcomes),
            tuple(item.probability for item in distribution.outcomes),
        )
        for key, distribution in conditional.items()
    }
    for _ in range(sample_count):
        outcome = SET_OUTCOME_ORDER[_sample_index(outcome_probabilities, rng)]
        home_sets, away_sets = _outcome_set_counts(outcome)
        regular_home, regular_away, deciding_home = _set_type_counts(home_sets, away_sets)
        scores: list[SetPointScore] = []
        for key, count in (
            (("regular", True), regular_home),
            (("regular", False), regular_away),
        ):
            choices, probabilities = score_tables[key]
            scores.extend(choices[_sample_index(probabilities, rng)] for _ in range(count))
        if deciding_home is not None:
            choices, probabilities = score_tables[("deciding", deciding_home)]
            scores.append(choices[_sample_index(probabilities, rng)])
        home_points = sum(score.home_points for score in scores)
        away_points = sum(score.away_points for score in scores)
        counts[(outcome, home_points, away_points)] += 1
    return {key: count / sample_count for key, count in counts.items()}


def _match_tail_bound(
    set_distribution: SetOutcomeDistribution,
    conditional: dict[tuple[str, bool], ConditionalSetScoreDistribution],
) -> tuple[float, float]:
    weighted_error = 0.0
    maximum_tail = max(
        (distribution.conditional_tail_probability for distribution in conditional.values()),
        default=0.0,
    )
    for outcome in SET_OUTCOME_ORDER:
        outcome_probability = set_distribution[outcome]
        if outcome_probability == 0:
            continue
        home_sets, away_sets = _outcome_set_counts(outcome)
        regular_home, regular_away, deciding_home = _set_type_counts(home_sets, away_sets)
        retained = 1.0
        if regular_home:
            retained *= (
                1.0 - conditional[("regular", True)].conditional_tail_probability
            ) ** regular_home
        if regular_away:
            retained *= (
                1.0 - conditional[("regular", False)].conditional_tail_probability
            ) ** regular_away
        if deciding_home is not None:
            retained *= 1.0 - conditional[("deciding", deciding_home)].conditional_tail_probability
        weighted_error += outcome_probability * (1.0 - retained)
    return weighted_error, maximum_tail


def _set_marginal(entries: tuple[JointScoreAtom, ...]) -> dict[SetOutcome, float]:
    totals = dict.fromkeys(SET_OUTCOME_ORDER, 0.0)
    for entry in entries:
        totals[entry.set_score] += entry.probability
    return totals


def _outcome_set_counts(outcome: SetOutcome) -> tuple[int, int]:
    home, away = outcome.value.split(":")
    return int(home), int(away)


def _set_type_counts(home_sets: int, away_sets: int) -> tuple[int, int, bool | None]:
    if home_sets + away_sets < 5:
        return home_sets, away_sets, None
    deciding_home = home_sets == 3
    return home_sets - int(deciding_home), away_sets - int(not deciding_home), deciding_home


def _convolve_probabilities(
    aggregate: dict[tuple[int, int], float],
    distribution: ConditionalSetScoreDistribution,
) -> dict[tuple[int, int], float]:
    combined: defaultdict[tuple[int, int], float] = defaultdict(float)
    for (home_total, away_total), aggregate_probability in aggregate.items():
        for item in distribution.outcomes:
            combined[
                (home_total + item.score.home_points, away_total + item.score.away_points)
            ] += aggregate_probability * item.probability
    return dict(combined)


def _valid_score_support(
    target: int,
    home_won: bool,
    max_deuce_cycles: int,
) -> set[tuple[int, int]]:
    support: set[tuple[int, int]] = set()
    for loser_points in range(target - 1):
        support.add((target, loser_points) if home_won else (loser_points, target))
    for cycle in range(max_deuce_cycles + 1):
        loser_points = target - 1 + cycle
        winner_points = loser_points + 2
        support.add((winner_points, loser_points) if home_won else (loser_points, winner_points))
    return support


def _convolve_support(
    aggregate: set[tuple[int, int]],
    score_support: set[tuple[int, int]],
) -> set[tuple[int, int]]:
    return {
        (home_total + home, away_total + away)
        for home_total, away_total in aggregate
        for home, away in score_support
    }


def _sample_index(probabilities: tuple[float, ...], rng: random.Random) -> int:
    threshold = rng.random()
    cumulative = 0.0
    for index, probability in enumerate(probabilities):
        cumulative += probability
        if threshold < cumulative:
            return index
    return len(probabilities) - 1


def _line_probabilities(
    values: MappingProxyType[int, float] | dict[float, float],
    line: float,
) -> LineProbabilities:
    selected_line = float(line)
    if not isfinite(selected_line):
        raise ValueError("line must be finite")
    above = sum(probability for value, probability in values.items() if value > selected_line)
    push = sum(probability for value, probability in values.items() if value == selected_line)
    below = sum(probability for value, probability in values.items() if value < selected_line)
    return LineProbabilities(above, push, below)


def _validate_probability(value: float, field_name: str) -> float:
    probability = float(value)
    if not isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError(f"{field_name} must be finite and between zero and one")
    return probability


def _require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _canonical_sha256(document: dict[str, Any]) -> str:
    payload = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()
