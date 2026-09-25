"""Chronological validation for joint volleyball score distributions."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import log
from types import MappingProxyType
from typing import Any

from vlytics.engine.experiments.baselines import CohortKey, TemporalSplit
from vlytics.engine.predictors.elo import Division, EloMatch
from vlytics.engine.predictors.points import (
    JointScorePrediction,
    JointScorePredictor,
    PointModelConfig,
    SetPointScore,
    VerifiedSeasonScoreRule,
    VerifiedSetPredictionArtifact,
    is_valid_set_score,
    score_rules_for_version,
)
from vlytics.engine.predictors.sets import SetOutcome

POINT_METRIC_VERSION = "joint-score-metrics-v1"
LOG_LOSS_EPSILON = 1e-12


@dataclass(frozen=True)
class ObservedMatchPoints:
    """One final, source-observed list of set scores for metric evaluation."""

    match_id: str
    division: Division
    set_scores: tuple[SetPointScore, ...]
    score_rule_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "division", Division(self.division))
        if not self.match_id.strip():
            raise ValueError("observed point result match_id must not be blank")
        rules = score_rules_for_version(self.score_rule_version)
        if not 3 <= len(self.set_scores) <= 5:
            raise ValueError("a completed best-of-five match has three to five sets")
        home_sets = 0
        away_sets = 0
        for index, score in enumerate(self.set_scores, start=1):
            if home_sets == rules.sets_to_win or away_sets == rules.sets_to_win:
                raise ValueError("set scores cannot continue after a match winner")
            if not is_valid_set_score(score, rules.target_for_set(index), rules.win_by):
                raise ValueError("observed result contains an invalid set score")
            home_sets += int(score.home_won)
            away_sets += int(not score.home_won)
        if max(home_sets, away_sets) != rules.sets_to_win:
            raise ValueError("observed set scores must produce a three-set match winner")

    @property
    def set_outcome(self) -> SetOutcome:
        home_sets = sum(score.home_won for score in self.set_scores)
        away_sets = len(self.set_scores) - home_sets
        return SetOutcome.from_result(home_sets, away_sets)

    @property
    def home_points(self) -> int:
        return sum(score.home_points for score in self.set_scores)

    @property
    def away_points(self) -> int:
        return sum(score.away_points for score in self.set_scores)


PointExperimentRow = tuple[
    EloMatch,
    VerifiedSetPredictionArtifact | None,
    ObservedMatchPoints | None,
]
ScoreRuleIdentityMap = Mapping[tuple[str, Division], VerifiedSeasonScoreRule]


@dataclass(frozen=True)
class JointScoreMetricReport:
    metric_version: str
    scheduled: int
    predicted: int
    result_available: int
    evaluated: int
    missing_predictions: int
    missing_results: int
    coverage: float
    joint_log_loss: float | None
    mean_observed_joint_probability: float | None
    point_total_crps: float | None
    point_differential_crps: float | None
    out_of_support_results: int
    predicted_constraint_violations: int
    mean_deuce_truncation_error_upper_bound: float | None
    maximum_source_set_marginal_error: float | None
    missing_reasons: MappingProxyType[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage,
            "evaluated": self.evaluated,
            "joint_log_loss": self.joint_log_loss,
            "maximum_source_set_marginal_error": self.maximum_source_set_marginal_error,
            "mean_deuce_truncation_error_upper_bound": (
                self.mean_deuce_truncation_error_upper_bound
            ),
            "mean_observed_joint_probability": self.mean_observed_joint_probability,
            "metric_version": self.metric_version,
            "missing_predictions": self.missing_predictions,
            "missing_reasons": dict(self.missing_reasons),
            "missing_results": self.missing_results,
            "out_of_support_results": self.out_of_support_results,
            "point_differential_crps": self.point_differential_crps,
            "point_total_crps": self.point_total_crps,
            "predicted": self.predicted,
            "predicted_constraint_violations": self.predicted_constraint_violations,
            "result_available": self.result_available,
            "scheduled": self.scheduled,
        }


@dataclass(frozen=True)
class JointScoreHoldoutReport:
    cohort: CohortKey
    split: TemporalSplit
    config: PointModelConfig
    validation_configuration_evidence: PointConfigurationEvidence
    test_configuration_evidence: PointConfigurationEvidence
    validation: JointScoreMetricReport
    test: JointScoreMetricReport
    test_configuration_frozen: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort.to_dict(),
            "config": self.config.to_dict(),
            "split": self.split.to_dict(),
            "test": self.test.to_dict(),
            "test_configuration_evidence": self.test_configuration_evidence.to_dict(),
            "test_configuration_frozen": self.test_configuration_frozen,
            "validation": self.validation.to_dict(),
            "validation_configuration_evidence": (self.validation_configuration_evidence.to_dict()),
        }


@dataclass(frozen=True)
class PointConfigurationEvidence:
    """Development rows available when the fixed point configuration was frozen."""

    frozen_at: datetime
    config_id: str
    development_match_ids: tuple[str, ...]
    development_manifest_sha256: str
    verified_rule_artifact_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_id": self.config_id,
            "development_manifest_sha256": self.development_manifest_sha256,
            "development_match_ids": list(self.development_match_ids),
            "frozen_at": self.frozen_at.isoformat(),
            "verified_rule_artifact_ids": list(self.verified_rule_artifact_ids),
        }


@dataclass(frozen=True)
class JointScoreHoldoutSuiteReport:
    reports: tuple[JointScoreHoldoutReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"reports": [report.to_dict() for report in self.reports]}


def evaluate_joint_score_predictions(
    matches: Sequence[EloMatch],
    predictions: Sequence[JointScorePrediction | None],
    results: Sequence[ObservedMatchPoints | None],
) -> JointScoreMetricReport:
    """Report proper score metrics, support failures, constraints, and sample sizes."""

    if len(matches) != len(predictions) or len(matches) != len(results):
        raise ValueError("matches, predictions, and results must have equal lengths")
    probabilities: list[float] = []
    total_crps: list[float] = []
    differential_crps: list[float] = []
    tail_errors: list[float] = []
    marginal_errors: list[float] = []
    predicted = 0
    result_available = 0
    out_of_support = 0
    constraint_violations = 0
    missing = Counter[str]()

    for match, prediction, result in zip(matches, predictions, results, strict=True):
        if result is not None:
            result_available += 1
            if result.match_id != match.match_id or result.division is not match.division:
                raise ValueError("observed result identity and division must match its match")
        else:
            missing["result_unavailable"] += 1
        if prediction is None:
            missing["prediction_unavailable"] += 1
            continue
        _validate_joint_prediction_identity(match, prediction)
        predicted += 1
        distribution = prediction.distribution
        rules = distribution.score_rules
        # Construction validates every atom against one cached reachable-score support.
        constraint_violations += 0
        tail_errors.append(distribution.diagnostics.deuce_truncation_error_upper_bound)
        marginal_errors.append(distribution.diagnostics.source_set_marginal_max_abs_error)
        if result is None:
            continue
        if result.score_rule_version != rules.version:
            raise ValueError("prediction and observed result score rules must match")
        probability = distribution.exact_probability(
            result.set_outcome,
            result.home_points,
            result.away_points,
        )
        probabilities.append(probability)
        out_of_support += int(probability == 0)
        total_crps.append(
            _discrete_crps(
                distribution.point_total_distribution,
                result.home_points + result.away_points,
            )
        )
        differential_crps.append(
            _discrete_crps(
                distribution.point_differential_distribution,
                result.home_points - result.away_points,
            )
        )

    scheduled = len(matches)
    evaluated = len(probabilities)
    return JointScoreMetricReport(
        metric_version=POINT_METRIC_VERSION,
        scheduled=scheduled,
        predicted=predicted,
        result_available=result_available,
        evaluated=evaluated,
        missing_predictions=scheduled - predicted,
        missing_results=scheduled - result_available,
        coverage=evaluated / scheduled if scheduled else 0.0,
        joint_log_loss=(
            -sum(log(max(value, LOG_LOSS_EPSILON)) for value in probabilities) / evaluated
            if evaluated
            else None
        ),
        mean_observed_joint_probability=(sum(probabilities) / evaluated if evaluated else None),
        point_total_crps=(sum(total_crps) / evaluated if evaluated else None),
        point_differential_crps=(sum(differential_crps) / evaluated if evaluated else None),
        out_of_support_results=out_of_support,
        predicted_constraint_violations=constraint_violations,
        mean_deuce_truncation_error_upper_bound=(
            sum(tail_errors) / len(tail_errors) if tail_errors else None
        ),
        maximum_source_set_marginal_error=(max(marginal_errors) if marginal_errors else None),
        missing_reasons=MappingProxyType(dict(sorted(missing.items()))),
    )


def run_joint_score_holdout(
    matches: Sequence[EloMatch],
    set_predictions: Sequence[VerifiedSetPredictionArtifact | None],
    results: Sequence[ObservedMatchPoints | None],
    split: TemporalSplit,
    config: PointModelConfig,
    *,
    producer_variant_id: str,
    score_rule_identities: ScoreRuleIdentityMap,
) -> JointScoreHoldoutReport:
    """Evaluate fixed point-model settings on chronological validation and test periods."""

    ordered = _ordered_rows(matches, set_predictions, results)
    cohort = _single_cohort(row[0] for row in ordered)
    train, validation, test = _partition(ordered, split)
    if not train or not validation or not test:
        raise ValueError("train, validation, and test periods must each contain matches")
    _validate_period_set_artifacts(
        validation,
        training_rows=train,
        expected_training_as_of=split.train_end,
    )
    _validate_period_set_artifacts(
        test,
        training_rows=train + validation,
        expected_training_as_of=split.validation_end,
    )
    predictor = JointScorePredictor(config, producer_variant_id=producer_variant_id)
    validation_predictions = _predict(predictor, validation, score_rule_identities)
    test_predictions = _predict(predictor, test, score_rule_identities)
    validation_evidence = _configuration_evidence(
        train,
        split.train_end,
        config,
        score_rule_identities,
    )
    test_evidence = _configuration_evidence(
        train + validation,
        split.validation_end,
        config,
        score_rule_identities,
    )
    return JointScoreHoldoutReport(
        cohort=cohort,
        split=split,
        config=config,
        validation_configuration_evidence=validation_evidence,
        test_configuration_evidence=test_evidence,
        validation=evaluate_joint_score_predictions(
            [row[0] for row in validation],
            validation_predictions,
            [row[2] for row in validation],
        ),
        test=evaluate_joint_score_predictions(
            [row[0] for row in test],
            test_predictions,
            [row[2] for row in test],
        ),
    )


def run_joint_score_holdout_suite(
    matches: Sequence[EloMatch],
    set_predictions: Sequence[VerifiedSetPredictionArtifact | None],
    results: Sequence[ObservedMatchPoints | None],
    split: TemporalSplit,
    config: PointModelConfig,
    *,
    producer_variant_id: str,
    score_rule_identities: ScoreRuleIdentityMap,
) -> JointScoreHoldoutSuiteReport:
    """Keep division and availability-policy cohorts separate."""

    rows = _ordered_rows(matches, set_predictions, results)
    grouped: defaultdict[
        CohortKey,
        list[PointExperimentRow],
    ] = defaultdict(list)
    for row in rows:
        grouped[CohortKey.from_match(row[0])].append(row)
    reports = tuple(
        run_joint_score_holdout(
            [row[0] for row in grouped[cohort]],
            [row[1] for row in grouped[cohort]],
            [row[2] for row in grouped[cohort]],
            split,
            config,
            producer_variant_id=producer_variant_id,
            score_rule_identities=score_rule_identities,
        )
        for cohort in sorted(grouped)
    )
    return JointScoreHoldoutSuiteReport(reports)


def _predict(
    predictor: JointScorePredictor,
    rows: Sequence[PointExperimentRow],
    score_rule_identities: ScoreRuleIdentityMap,
) -> list[JointScorePrediction | None]:
    predictions: list[JointScorePrediction | None] = []
    for match, artifact, _ in rows:
        if artifact is None:
            predictions.append(None)
            continue
        rule_identity = _score_rule_identity(match, score_rule_identities)
        predictions.append(predictor.predict(artifact, rule_identity))
    return predictions


def _validate_joint_prediction_identity(
    match: EloMatch,
    prediction: JointScorePrediction,
) -> None:
    artifact = prediction.source_set_artifact
    _validate_set_artifact_identity(match, artifact)
    rule_identity = prediction.distribution.score_rule_identity
    mismatched: list[str] = []
    if prediction.match_id != match.match_id:
        mismatched.append("match_id")
    if prediction.division is not match.division:
        mismatched.append("division")
    if rule_identity.season_id != match.season_id:
        mismatched.append("season_rule")
    if rule_identity.division is not match.division:
        mismatched.append("rule_division")
    if rule_identity.verified_at > match.prediction_cutoff_at:
        mismatched.append("rule_verified_at")
    if mismatched:
        raise ValueError("joint score prediction identity mismatch: " + ", ".join(mismatched))


def _validate_set_artifact_identity(
    match: EloMatch,
    artifact: VerifiedSetPredictionArtifact,
) -> None:
    prediction = artifact.prediction
    mismatched: list[str] = []
    if prediction.match_id != match.match_id:
        mismatched.append("match_id")
    if prediction.schedule_revision_id != match.schedule_revision_id:
        mismatched.append("schedule_revision_id")
    if prediction.prediction_cutoff_at != match.prediction_cutoff_at:
        mismatched.append("prediction_cutoff_at")
    if prediction.division is not match.division:
        mismatched.append("division")
    if prediction.training_cohort.to_dict() != CohortKey.from_match(match).to_dict():
        mismatched.append("training_cohort")
    if prediction.training_as_of > match.prediction_cutoff_at:
        mismatched.append("training_as_of")
    if mismatched:
        raise ValueError("source set prediction identity mismatch: " + ", ".join(mismatched))


def _validate_period_set_artifacts(
    rows: Sequence[PointExperimentRow],
    *,
    training_rows: Sequence[PointExperimentRow],
    expected_training_as_of: datetime,
) -> None:
    expected_manifest = _training_result_manifest_sha256(
        [row[0] for row in training_rows],
        expected_training_as_of,
    )
    frozen_lineages: set[tuple[Any, ...]] = set()
    for match, artifact, _ in rows:
        if artifact is None:
            continue
        _validate_set_artifact_identity(match, artifact)
        prediction = artifact.prediction
        if prediction.training_as_of != expected_training_as_of:
            raise ValueError("set prediction training_as_of does not match the split boundary")
        if prediction.training_result_manifest_sha256 != expected_manifest:
            raise ValueError("set prediction training manifest does not match prior rows")
        frozen_lineages.add(
            (
                prediction.model_version,
                prediction.distribution_version,
                prediction.config_id,
                prediction.random_seed,
                prediction.fitted_parameter_artifact_id,
                json.dumps(prediction.training_cohort.to_dict(), sort_keys=True),
                prediction.training_as_of,
                prediction.training_result_manifest_sha256,
                prediction.source_model_version,
                prediction.source_config_id,
            )
        )
    if len(frozen_lineages) > 1:
        raise ValueError("set predictions in one holdout period must share frozen lineage")


def _configuration_evidence(
    development_rows: Sequence[PointExperimentRow],
    frozen_at: datetime,
    config: PointModelConfig,
    score_rule_identities: ScoreRuleIdentityMap,
) -> PointConfigurationEvidence:
    manifest: list[dict[str, Any]] = []
    rule_ids: set[str] = set()
    for match, artifact, _ in development_rows:
        rule = _score_rule_identity(match, score_rule_identities)
        rule_ids.add(rule.mirror_rule_artifact_id)
        manifest.append(
            {
                "input_snapshot_id": artifact.input_snapshot_id if artifact else None,
                "match_id": match.match_id,
                "prediction_cutoff_at": match.prediction_cutoff_at.isoformat(),
                "schedule_revision_id": match.schedule_revision_id,
                "set_prediction_id": artifact.prediction_id if artifact else None,
                "verified_rule_artifact_id": rule.mirror_rule_artifact_id,
            }
        )
    return PointConfigurationEvidence(
        frozen_at=frozen_at,
        config_id=config.config_id,
        development_match_ids=tuple(item["match_id"] for item in manifest),
        development_manifest_sha256=_canonical_sha256(manifest),
        verified_rule_artifact_ids=tuple(sorted(rule_ids)),
    )


def _score_rule_identity(
    match: EloMatch,
    identities: ScoreRuleIdentityMap,
) -> VerifiedSeasonScoreRule:
    key = (match.season_id, match.division)
    try:
        identity = identities[key]
    except KeyError as error:
        raise ValueError(
            "verified season score rule identity is required for "
            f"{match.season_id}/{match.division.value}"
        ) from error
    if identity.season_id != match.season_id or identity.division is not match.division:
        raise ValueError("verified season score rule identity key is inconsistent")
    if identity.verified_at > match.prediction_cutoff_at:
        raise ValueError("score rule identity was verified after the prediction cutoff")
    return identity


def _training_result_manifest_sha256(
    matches: Sequence[EloMatch],
    known_at: datetime,
) -> str:
    manifest: list[dict[str, Any]] = []
    for match in sorted(matches, key=lambda item: item.order_key):
        result = match.select_result_as_of(known_at)
        if result is None:
            continue
        manifest.append(
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
    return _canonical_sha256(manifest)


def _discrete_crps(distribution: MappingProxyType[int, float], observation: int) -> float:
    lower = min(min(distribution), observation)
    upper = max(max(distribution), observation)
    cumulative = 0.0
    total = 0.0
    for value in range(lower, upper + 1):
        cumulative += distribution.get(value, 0.0)
        observed_cumulative = float(value >= observation)
        total += (cumulative - observed_cumulative) ** 2
    return total


def _ordered_rows(
    matches: Sequence[EloMatch],
    set_predictions: Sequence[VerifiedSetPredictionArtifact | None],
    results: Sequence[ObservedMatchPoints | None],
) -> list[PointExperimentRow]:
    if len(matches) != len(set_predictions) or len(matches) != len(results):
        raise ValueError("matches, set_predictions, and results must have equal lengths")
    if len({match.match_id for match in matches}) != len(matches):
        raise ValueError("match_id must be unique within an experiment")
    return sorted(
        zip(matches, set_predictions, results, strict=True),
        key=lambda row: row[0].order_key,
    )


def _single_cohort(matches: Iterable[EloMatch]) -> CohortKey:
    cohorts = {CohortKey.from_match(match) for match in matches}
    if not cohorts:
        raise ValueError("an experiment requires matches")
    if len(cohorts) != 1:
        raise ValueError("division and availability-policy cohorts must remain separate")
    return next(iter(cohorts))


def _partition(
    rows: Sequence[PointExperimentRow],
    split: TemporalSplit,
) -> tuple[
    list[PointExperimentRow],
    list[PointExperimentRow],
    list[PointExperimentRow],
]:
    train: list[PointExperimentRow] = []
    validation: list[PointExperimentRow] = []
    test: list[PointExperimentRow] = []
    outside: list[str] = []
    for row in rows:
        cutoff = row[0].prediction_cutoff_at
        if split.train_start <= cutoff < split.train_end:
            train.append(row)
        elif split.validation_start <= cutoff < split.validation_end:
            validation.append(row)
        elif split.test_start <= cutoff < split.test_end:
            test.append(row)
        else:
            outside.append(row[0].match_id)
    if outside:
        raise ValueError("matches outside the declared split: " + ", ".join(sorted(outside)))
    return train, validation, test


def _canonical_sha256(document: Any) -> str:
    payload = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()
