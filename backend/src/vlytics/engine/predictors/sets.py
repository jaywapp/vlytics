"""Explainable six-outcome volleyball set-score probabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any

from vlytics.engine.predictors.elo import Division, EloMatch, EloPrediction

DEFAULT_SET_MODEL_VERSION = "independent-sets-v2"
DEFAULT_SET_DISTRIBUTION_VERSION = "set-score-six-v1"
DEFAULT_SET_RANDOM_SEED = 0
MASS_TOLERANCE = 1e-12
MARGINAL_TOLERANCE = 1e-12


class PredictionCapability(StrEnum):
    WINNER = "winner"
    SET_SCORE = "set_score"
    POINT_HANDICAP = "point_handicap"
    POINT_TOTAL = "point_total"


class SetOutcome(StrEnum):
    HOME_3_0 = "3:0"
    HOME_3_1 = "3:1"
    HOME_3_2 = "3:2"
    AWAY_3_2 = "2:3"
    AWAY_3_1 = "1:3"
    AWAY_3_0 = "0:3"

    @classmethod
    def from_result(cls, home_sets: int, away_sets: int) -> SetOutcome:
        try:
            return cls(f"{home_sets}:{away_sets}")
        except ValueError as error:
            raise ValueError("result must be one of the six best-of-five set scores") from error


SET_OUTCOME_ORDER: tuple[SetOutcome, ...] = (
    SetOutcome.HOME_3_0,
    SetOutcome.HOME_3_1,
    SetOutcome.HOME_3_2,
    SetOutcome.AWAY_3_2,
    SetOutcome.AWAY_3_1,
    SetOutcome.AWAY_3_0,
)


@dataclass(frozen=True)
class SetOutcomeProbability:
    outcome: SetOutcome
    probability: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", SetOutcome(self.outcome))
        object.__setattr__(self, "probability", float(self.probability))
        if not isfinite(self.probability) or not 0 <= self.probability <= 1:
            raise ValueError("set outcome probabilities must be finite and between zero and one")


@dataclass(frozen=True)
class SetOutcomeDistribution:
    """Exactly six probabilities in the public home-first order."""

    outcomes: tuple[SetOutcomeProbability, ...]

    def __post_init__(self) -> None:
        if len(self.outcomes) != len(SET_OUTCOME_ORDER):
            raise ValueError("a set distribution must contain exactly six outcomes")
        if tuple(item.outcome for item in self.outcomes) != SET_OUTCOME_ORDER:
            raise ValueError("set outcomes must use the fixed 3:0 through 0:3 order")
        if abs(sum(self.probabilities) - 1.0) > MASS_TOLERANCE:
            raise ValueError("set outcome probability mass must sum to one")

    @property
    def probabilities(self) -> tuple[float, ...]:
        return tuple(item.probability for item in self.outcomes)

    @property
    def home_win_probability(self) -> float:
        return sum(self.probabilities[:3])

    def __getitem__(self, outcome: SetOutcome | str) -> float:
        expected = SetOutcome(outcome)
        return self.outcomes[SET_OUTCOME_ORDER.index(expected)].probability

    def as_mapping(self) -> MappingProxyType[str, float]:
        return MappingProxyType({item.outcome.value: item.probability for item in self.outcomes})

    def to_list(self) -> list[dict[str, float | str]]:
        return [
            {"outcome": item.outcome.value, "probability": item.probability}
            for item in self.outcomes
        ]


@dataclass(frozen=True, order=True)
class SetTrainingCohort:
    """The complete Elo cohort key copied into a fitted p5 artifact."""

    division: Division
    availability_policy: str
    competition: str
    stage: str
    timing_eligibility: str
    result_finality_policy: str
    input_version: str
    franchise_mapping_version: str

    @classmethod
    def from_match(cls, match: EloMatch) -> SetTrainingCohort:
        return cls(
            division=match.division,
            availability_policy=match.availability_policy.value,
            competition=match.competition,
            stage=match.stage,
            timing_eligibility=match.timing_eligibility.value,
            result_finality_policy=match.result_finality_policy.value,
            input_version=match.input_version,
            franchise_mapping_version=match.franchise_mapping_version,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "availability_policy": self.availability_policy,
            "competition": self.competition,
            "division": self.division.value,
            "franchise_mapping_version": self.franchise_mapping_version,
            "input_version": self.input_version,
            "result_finality_policy": self.result_finality_policy,
            "stage": self.stage,
            "timing_eligibility": self.timing_eligibility,
        }


@dataclass(frozen=True)
class SetModelConfig:
    fifth_set_prior_home_wins: float = 1.0
    fifth_set_prior_away_wins: float = 1.0
    model_version: str = DEFAULT_SET_MODEL_VERSION
    distribution_version: str = DEFAULT_SET_DISTRIBUTION_VERSION
    random_seed: int = DEFAULT_SET_RANDOM_SEED

    def __post_init__(self) -> None:
        for name in ("fifth_set_prior_home_wins", "fifth_set_prior_away_wins"):
            object.__setattr__(self, name, float(getattr(self, name)))
        priors = (self.fifth_set_prior_home_wins, self.fifth_set_prior_away_wins)
        if any(not isfinite(value) or value < 0 for value in priors):
            raise ValueError("fifth-set prior counts must be finite and non-negative")
        if sum(priors) <= 0:
            raise ValueError("fifth-set prior counts must have positive total mass")
        if not self.model_version.strip() or not self.distribution_version.strip():
            raise ValueError("model and distribution versions must not be blank")
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise ValueError("random_seed must be an integer")

    @property
    def capabilities(self) -> tuple[PredictionCapability, ...]:
        return (PredictionCapability.WINNER, PredictionCapability.SET_SCORE)

    @property
    def config_id(self) -> str:
        return _canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "capabilities": [item.value for item in self.capabilities],
            "distribution_version": self.distribution_version,
            "fifth_set_prior_away_wins": self.fifth_set_prior_away_wins,
            "fifth_set_prior_home_wins": self.fifth_set_prior_home_wins,
            "model_version": self.model_version,
            "random_seed": self.random_seed,
        }


@dataclass(frozen=True)
class SetModelParameters:
    training_cohort: SetTrainingCohort
    fifth_set_home_win_probability: float
    fifth_set_home_wins: int
    fifth_set_matches: int
    completed_training_matches: int
    model_version: str
    distribution_version: str
    base_config_id: str
    config_id: str
    random_seed: int
    capabilities: tuple[PredictionCapability, ...]
    fitted_parameter_artifact_id: str
    training_as_of: datetime
    training_result_manifest_sha256: str
    training_result_revision_ids: tuple[str, ...]

    @property
    def division(self) -> Division:
        return self.training_cohort.division

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_config_id": self.base_config_id,
            "capabilities": [item.value for item in self.capabilities],
            "completed_training_matches": self.completed_training_matches,
            "config_id": self.config_id,
            "distribution_version": self.distribution_version,
            "fifth_set_home_win_probability": self.fifth_set_home_win_probability,
            "fifth_set_home_wins": self.fifth_set_home_wins,
            "fifth_set_matches": self.fifth_set_matches,
            "fitted_parameter_artifact_id": self.fitted_parameter_artifact_id,
            "model_version": self.model_version,
            "random_seed": self.random_seed,
            "training_as_of": self.training_as_of.isoformat(),
            "training_cohort": self.training_cohort.to_dict(),
            "training_result_manifest_sha256": self.training_result_manifest_sha256,
            "training_result_revision_ids": list(self.training_result_revision_ids),
        }


@dataclass(frozen=True)
class SetOutcomePrediction:
    match_id: str
    schedule_revision_id: str
    prediction_cutoff_at: datetime
    division: Division
    source_home_win_probability: float
    source_prediction_id: str
    source_model_version: str
    source_config_id: str
    regular_set_home_win_probability: float
    fifth_set_home_win_probability: float
    distribution: SetOutcomeDistribution
    model_version: str
    distribution_version: str
    config_id: str
    random_seed: int
    capabilities: tuple[PredictionCapability, ...]
    fitted_parameter_artifact_id: str
    training_cohort: SetTrainingCohort
    training_as_of: datetime
    training_result_manifest_sha256: str
    assumption: str = "sets_1_to_4_are_conditionally_independent_with_constant_p"

    @property
    def home_win_probability(self) -> float:
        return self.distribution.home_win_probability

    def __post_init__(self) -> None:
        if abs(self.home_win_probability - self.source_home_win_probability) > MARGINAL_TOLERANCE:
            raise ValueError("set distribution winner marginal must match its source probability")
        for field_name in (
            "source_prediction_id",
            "source_model_version",
            "source_config_id",
            "fitted_parameter_artifact_id",
            "training_result_manifest_sha256",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must not be blank")

    def statistical_lineage(self) -> dict[str, Any]:
        return {
            "fitted_parameter_artifact_id": self.fitted_parameter_artifact_id,
            "training_as_of": self.training_as_of.isoformat(),
            "training_cohort": self.training_cohort.to_dict(),
            "training_result_manifest_sha256": self.training_result_manifest_sha256,
            "upstream_config_id": self.source_config_id,
            "upstream_model_version": self.source_model_version,
            "upstream_prediction_id": self.source_prediction_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "assumption": self.assumption,
            "capabilities": [item.value for item in self.capabilities],
            "config_id": self.config_id,
            "distribution_version": self.distribution_version,
            "division": self.division.value,
            "fifth_set_home_win_probability": self.fifth_set_home_win_probability,
            "home_win_probability": self.home_win_probability,
            "match_id": self.match_id,
            "model_version": self.model_version,
            "prediction_cutoff_at": self.prediction_cutoff_at.isoformat(),
            "random_seed": self.random_seed,
            "regular_set_home_win_probability": self.regular_set_home_win_probability,
            "schedule_revision_id": self.schedule_revision_id,
            "set_score_probabilities": self.distribution.to_list(),
            "source_home_win_probability": self.source_home_win_probability,
            **self.statistical_lineage(),
        }

    def to_contract(
        self,
        *,
        producer_variant_id: str,
        input_snapshot_id: str,
    ) -> dict[str, Any]:
        if not producer_variant_id.strip() or not input_snapshot_id.strip():
            raise ValueError("producer_variant_id and input_snapshot_id must not be blank")
        provenance: dict[str, Any] = {
            "producer_variant_id": producer_variant_id,
            "distribution_version": self.distribution_version,
            "probability_source": "statistical_derived",
            "derivation_method": "independent_set_distribution_marginalized_to_winner",
            "input_snapshot_id": input_snapshot_id,
            **self.statistical_lineage(),
        }
        return {
            "schema_version": "prediction-v1",
            "producer_variant_id": producer_variant_id,
            "model_version": self.model_version,
            "distribution_version": self.distribution_version,
            "config_id": self.config_id,
            "random_seed": self.random_seed,
            "division": self.division.value,
            "capabilities": [item.value for item in self.capabilities],
            "home_win_probability": self.home_win_probability,
            "set_score_probabilities": self.distribution.to_list(),
            "target_provenance": {
                "winner": dict(provenance),
                "set_score": dict(provenance),
            },
            "rationale": (
                "A cohort-specific fifth-set rate is combined with a constant probability "
                "for sets one through four; that probability is solved so the winner marginal "
                "matches the validated upstream Elo prediction."
            ),
            "risk_factors": [self.assumption],
        }


class SetOutcomePredictor:
    def __init__(self, parameters: SetModelParameters) -> None:
        self.parameters = parameters

    @classmethod
    def fit(
        cls,
        matches: Sequence[EloMatch],
        config: SetModelConfig | None = None,
        *,
        known_at: datetime,
    ) -> SetOutcomePredictor:
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ValueError("known_at must be timezone-aware")
        if not matches:
            raise ValueError("set model fitting requires training matches")
        cohorts = {SetTrainingCohort.from_match(match) for match in matches}
        if len(cohorts) != 1:
            raise ValueError("set model training requires one complete CohortKey")
        training_cohort = next(iter(cohorts))
        if any(match.prediction_cutoff_at >= known_at for match in matches):
            raise ValueError("training matches must precede the fitted parameter as-of time")
        selected_config = config or SetModelConfig()
        fifth_set_matches = 0
        fifth_set_home_wins = 0
        result_manifest: list[dict[str, Any]] = []
        for match in sorted(matches, key=lambda item: item.order_key):
            result = match.select_result_as_of(known_at)
            if result is None:
                continue
            result_manifest.append(
                {
                    "away_sets": result.away_sets,
                    "finalized_at": result.finalized_at.isoformat(),
                    "home_sets": result.home_sets,
                    "match_id": match.match_id,
                    "observed_at": result.observed_at.isoformat(),
                    "raw_snapshot_sha256": result.raw_snapshot_sha256,
                    "revision_id": result.revision_id,
                }
            )
            if result.home_sets + result.away_sets == 5:
                fifth_set_matches += 1
                fifth_set_home_wins += int(result.home_sets == 3)
        numerator = fifth_set_home_wins + selected_config.fifth_set_prior_home_wins
        denominator = (
            fifth_set_matches
            + selected_config.fifth_set_prior_home_wins
            + selected_config.fifth_set_prior_away_wins
        )
        p5 = numerator / denominator
        manifest_sha256 = _canonical_sha256(result_manifest)
        artifact_document = {
            "base_config_id": selected_config.config_id,
            "completed_training_matches": len(result_manifest),
            "fifth_set_home_win_probability": p5,
            "fifth_set_home_wins": fifth_set_home_wins,
            "fifth_set_matches": fifth_set_matches,
            "training_as_of": known_at.isoformat(),
            "training_cohort": training_cohort.to_dict(),
            "training_result_manifest_sha256": manifest_sha256,
        }
        artifact_id = _canonical_sha256(artifact_document)
        fitted_config_id = _canonical_sha256(
            {
                "base_config_id": selected_config.config_id,
                "fitted_parameter_artifact_id": artifact_id,
            }
        )
        return cls(
            SetModelParameters(
                training_cohort=training_cohort,
                fifth_set_home_win_probability=p5,
                fifth_set_home_wins=fifth_set_home_wins,
                fifth_set_matches=fifth_set_matches,
                completed_training_matches=len(result_manifest),
                model_version=selected_config.model_version,
                distribution_version=selected_config.distribution_version,
                base_config_id=selected_config.config_id,
                config_id=fitted_config_id,
                random_seed=selected_config.random_seed,
                capabilities=selected_config.capabilities,
                fitted_parameter_artifact_id=artifact_id,
                training_as_of=known_at,
                training_result_manifest_sha256=manifest_sha256,
                training_result_revision_ids=tuple(
                    str(item["revision_id"]) for item in result_manifest
                ),
            )
        )

    def predict(
        self,
        match: EloMatch,
        winner_prediction: EloPrediction,
        *,
        upstream_prediction_id: str | None = None,
    ) -> SetOutcomePrediction:
        self._validate_upstream(match, winner_prediction)
        prediction_id = upstream_prediction_id or elo_prediction_artifact_id(winner_prediction)
        if not prediction_id.strip():
            raise ValueError("upstream_prediction_id must not be blank")
        p5 = self.parameters.fifth_set_home_win_probability
        p = solve_regular_set_probability(winner_prediction.home_win_probability, p5)
        distribution = independent_set_distribution(p, p5)
        config_id = _canonical_sha256(
            {
                "fitted_set_config_id": self.parameters.config_id,
                "fitted_parameter_artifact_id": self.parameters.fitted_parameter_artifact_id,
                "upstream_config_id": winner_prediction.config_id,
                "upstream_model_version": winner_prediction.model_version,
                "upstream_prediction_id": prediction_id,
            }
        )
        return SetOutcomePrediction(
            match_id=match.match_id,
            schedule_revision_id=match.schedule_revision_id,
            prediction_cutoff_at=match.prediction_cutoff_at,
            division=match.division,
            source_home_win_probability=winner_prediction.home_win_probability,
            source_prediction_id=prediction_id,
            source_model_version=winner_prediction.model_version,
            source_config_id=winner_prediction.config_id,
            regular_set_home_win_probability=p,
            fifth_set_home_win_probability=p5,
            distribution=distribution,
            model_version=self.parameters.model_version,
            distribution_version=self.parameters.distribution_version,
            config_id=config_id,
            random_seed=self.parameters.random_seed,
            capabilities=self.parameters.capabilities,
            fitted_parameter_artifact_id=self.parameters.fitted_parameter_artifact_id,
            training_cohort=self.parameters.training_cohort,
            training_as_of=self.parameters.training_as_of,
            training_result_manifest_sha256=(self.parameters.training_result_manifest_sha256),
        )

    def _validate_upstream(self, match: EloMatch, prediction: EloPrediction) -> None:
        expected = {
            "availability_policy": match.availability_policy,
            "competition": match.competition,
            "division": match.division,
            "franchise_mapping_version": match.franchise_mapping_version,
            "input_version": match.input_version,
            "match_id": match.match_id,
            "prediction_cutoff_at": match.prediction_cutoff_at,
            "schedule_raw_snapshot_sha256": match.schedule_raw_snapshot_sha256,
            "schedule_revision_id": match.schedule_revision_id,
            "season_id": match.season_id,
            "stage": match.stage,
        }
        mismatched = [
            name for name, value in expected.items() if getattr(prediction, name) != value
        ]
        if mismatched:
            raise ValueError("upstream Elo prediction identity mismatch: " + ", ".join(mismatched))
        if SetTrainingCohort.from_match(match) != self.parameters.training_cohort:
            raise ValueError("prediction match is outside the fitted p5 CohortKey")
        if match.prediction_cutoff_at < self.parameters.training_as_of:
            raise ValueError("fitted p5 artifact was created after the prediction cutoff")
        _validate_probability(prediction.home_win_probability, "home_win_probability")
        if not prediction.model_version.strip() or not prediction.config_id.strip():
            raise ValueError("upstream Elo model and config lineage must not be blank")


def elo_prediction_artifact_id(prediction: EloPrediction) -> str:
    return _canonical_sha256(
        {
            "availability_policy": prediction.availability_policy.value,
            "away_franchise_id": prediction.away_franchise_id,
            "away_rating": prediction.away_rating,
            "competition": prediction.competition,
            "config_id": prediction.config_id,
            "division": prediction.division.value,
            "franchise_mapping_version": prediction.franchise_mapping_version,
            "home_franchise_id": prediction.home_franchise_id,
            "home_rating": prediction.home_rating,
            "home_win_probability": prediction.home_win_probability,
            "input_version": prediction.input_version,
            "match_id": prediction.match_id,
            "model_version": prediction.model_version,
            "prediction_cutoff_at": prediction.prediction_cutoff_at.isoformat(),
            "random_seed": prediction.random_seed,
            "schedule_raw_snapshot_sha256": prediction.schedule_raw_snapshot_sha256,
            "schedule_revision_id": prediction.schedule_revision_id,
            "season_id": prediction.season_id,
            "stage": prediction.stage,
        }
    )


def independent_set_distribution(
    regular_set_home_win_probability: float,
    fifth_set_home_win_probability: float,
) -> SetOutcomeDistribution:
    p = _validate_probability(
        regular_set_home_win_probability,
        "regular_set_home_win_probability",
    )
    p5 = _validate_probability(
        fifth_set_home_win_probability,
        "fifth_set_home_win_probability",
    )
    q = 1.0 - p
    deciding_prefix = 6.0 * p**2 * q**2
    probabilities = (
        p**3,
        3.0 * p**3 * q,
        deciding_prefix * p5,
        deciding_prefix * (1.0 - p5),
        3.0 * q**3 * p,
        q**3,
    )
    return SetOutcomeDistribution(
        tuple(
            SetOutcomeProbability(outcome, probability)
            for outcome, probability in zip(SET_OUTCOME_ORDER, probabilities, strict=True)
        )
    )


def solve_regular_set_probability(
    home_win_probability: float,
    fifth_set_home_win_probability: float,
) -> float:
    target = _validate_probability(home_win_probability, "home_win_probability")
    p5 = _validate_probability(
        fifth_set_home_win_probability,
        "fifth_set_home_win_probability",
    )
    if target in {0.0, 1.0}:
        return target
    low = 0.0
    high = 1.0
    for _ in range(80):
        midpoint = (low + high) / 2.0
        if _winner_marginal(midpoint, p5) < target:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2.0


def _winner_marginal(p: float, p5: float) -> float:
    q = 1.0 - p
    return p**3 + 3.0 * p**3 * q + 6.0 * p**2 * q**2 * p5


def _validate_probability(value: float, field_name: str) -> float:
    probability = float(value)
    if not isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError(f"{field_name} must be finite and between zero and one")
    return probability


def _canonical_sha256(document: Mapping[str, Any] | Sequence[Any]) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
