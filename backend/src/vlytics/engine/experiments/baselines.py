"""Leakage-safe selection and frozen walk-forward evaluation for baselines."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, log
from types import MappingProxyType
from typing import Any

from vlytics.engine.features import AvailabilityPolicy
from vlytics.engine.predictors.elo import (
    Division,
    EloConfig,
    EloMatch,
    EloPrediction,
    EloPredictor,
    EloResultRevision,
    ResultFinalityPolicy,
    TimingEligibility,
)

METRIC_VERSION = "binary-probability-metrics-v2"
LOG_LOSS_EPSILON = 1e-12


@dataclass(frozen=True)
class TemporalSplit:
    """Explicit, contiguous, half-open train/validation/test boundaries."""

    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    test_start: datetime
    test_end: datetime

    def __post_init__(self) -> None:
        boundaries = (
            self.train_start,
            self.train_end,
            self.validation_start,
            self.validation_end,
            self.test_start,
            self.test_end,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in boundaries):
            raise ValueError("split boundaries must be timezone-aware")
        if not self.train_start < self.train_end:
            raise ValueError("train_start must precede train_end")
        if self.validation_start != self.train_end:
            raise ValueError("validation must start exactly when training ends")
        if not self.validation_start < self.validation_end:
            raise ValueError("validation_start must precede validation_end")
        if self.test_start != self.validation_end:
            raise ValueError("test must start exactly when validation ends")
        if not self.test_start < self.test_end:
            raise ValueError("test_start must precede test_end")

    def to_dict(self) -> dict[str, str]:
        return {
            "test_end": self.test_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "train_start": self.train_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
        }


@dataclass(frozen=True, order=True)
class CohortKey:
    """Dimensions that must be homogeneous for selection and metrics."""

    division: Division
    availability_policy: AvailabilityPolicy
    competition: str
    stage: str
    timing_eligibility: TimingEligibility
    result_finality_policy: ResultFinalityPolicy
    input_version: str
    franchise_mapping_version: str

    @classmethod
    def from_match(cls, match: EloMatch) -> CohortKey:
        return cls(
            division=match.division,
            availability_policy=match.availability_policy,
            competition=match.competition,
            stage=match.stage,
            timing_eligibility=match.timing_eligibility,
            result_finality_policy=match.result_finality_policy,
            input_version=match.input_version,
            franchise_mapping_version=match.franchise_mapping_version,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "availability_policy": self.availability_policy.value,
            "competition": self.competition,
            "division": self.division.value,
            "franchise_mapping_version": self.franchise_mapping_version,
            "input_version": self.input_version,
            "result_finality_policy": self.result_finality_policy.value,
            "stage": self.stage,
            "timing_eligibility": self.timing_eligibility.value,
        }


@dataclass(frozen=True)
class InputManifestEntry:
    position: int
    match: EloMatch

    def to_dict(self) -> dict[str, Any]:
        return {"position": self.position, **self.match.to_manifest_dict()}


@dataclass(frozen=True)
class InputManifest:
    entries: tuple[InputManifestEntry, ...]
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class MetricReport:
    metric_version: str
    cohort: CohortKey
    evaluation_result_known_at: datetime | None
    scheduled: int
    predicted: int
    result_available: int
    evaluated: int
    missing_predictions: int
    missing_results: int
    coverage: float
    brier_score: float | None
    log_loss: float | None
    accuracy: float | None
    missing_reasons: MappingProxyType[str, int]
    result_revision_ids: tuple[str, ...]
    result_raw_snapshot_sha256: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accuracy": self.accuracy,
            "brier_score": self.brier_score,
            "cohort": self.cohort.to_dict(),
            "coverage": self.coverage,
            "evaluated": self.evaluated,
            "evaluation_result_known_at": (
                self.evaluation_result_known_at.isoformat()
                if self.evaluation_result_known_at
                else None
            ),
            "log_loss": self.log_loss,
            "metric_version": self.metric_version,
            "missing_predictions": self.missing_predictions,
            "missing_reasons": dict(self.missing_reasons),
            "missing_results": self.missing_results,
            "predicted": self.predicted,
            "result_available": self.result_available,
            "result_raw_snapshot_sha256": list(self.result_raw_snapshot_sha256),
            "result_revision_ids": list(self.result_revision_ids),
            "scheduled": self.scheduled,
        }


@dataclass(frozen=True)
class CandidateValidation:
    config: EloConfig
    metrics: MetricReport


@dataclass(frozen=True)
class BaselineExperimentReport:
    cohort: CohortKey
    split: TemporalSplit
    input_manifest: InputManifest
    selected_config: EloConfig
    candidate_validation: tuple[CandidateValidation, ...]
    elo_test: MetricReport
    home_win_probability: float | None
    home_training_sample_size: int
    home_training_wins: int
    home_training_result_revision_ids: tuple[str, ...]
    home_validation: MetricReport
    home_test: MetricReport
    selection_data_ends_before: datetime
    test_started_at: datetime
    test_parameters_frozen: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_validation": [
                {
                    "config": item.config.to_dict(),
                    "config_id": item.config.config_id,
                    "metrics": item.metrics.to_dict(),
                }
                for item in self.candidate_validation
            ],
            "cohort": self.cohort.to_dict(),
            "elo_test": self.elo_test.to_dict(),
            "home_test": self.home_test.to_dict(),
            "home_training_result_revision_ids": list(self.home_training_result_revision_ids),
            "home_training_sample_size": self.home_training_sample_size,
            "home_training_wins": self.home_training_wins,
            "home_validation": self.home_validation.to_dict(),
            "home_win_probability": self.home_win_probability,
            "input_manifest": self.input_manifest.to_dict(),
            "selected_config": self.selected_config.to_dict(),
            "selected_config_id": self.selected_config.config_id,
            "selection_data_ends_before": self.selection_data_ends_before.isoformat(),
            "split": self.split.to_dict(),
            "test_parameters_frozen": self.test_parameters_frozen,
            "test_started_at": self.test_started_at.isoformat(),
        }


@dataclass(frozen=True)
class BaselineSuiteReport:
    reports: tuple[BaselineExperimentReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"reports": [report.to_dict() for report in self.reports]}


class HomeWinRateBaseline:
    """A constant probability learned from results known by the train boundary."""

    def __init__(self) -> None:
        self._probability: float | None = None
        self._cohort: CohortKey | None = None
        self._sample_size = 0
        self._home_wins = 0
        self._result_revision_ids: tuple[str, ...] = ()

    @property
    def probability(self) -> float | None:
        return self._probability

    @property
    def sample_size(self) -> int:
        return self._sample_size

    @property
    def home_wins(self) -> int:
        return self._home_wins

    @property
    def result_revision_ids(self) -> tuple[str, ...]:
        return self._result_revision_ids

    def fit(self, matches: Sequence[EloMatch], *, known_at: datetime) -> None:
        cohort = _require_single_cohort(matches, allow_empty=True)
        selected = [
            result
            for match in matches
            if (result := match.select_result_as_of(known_at)) is not None
        ]
        self._sample_size = len(selected)
        self._home_wins = int(sum(result.outcome for result in selected))
        self._probability = self._home_wins / len(selected) if selected else None
        self._result_revision_ids = tuple(result.revision_id for result in selected)
        self._cohort = cohort

    def predict(self, match: EloMatch) -> float | None:
        cohort = CohortKey.from_match(match)
        if self._cohort is not None and cohort != self._cohort:
            raise ValueError("home-win baseline cannot cross experiment cohorts")
        return self._probability


def default_candidate_grid() -> tuple[EloConfig, ...]:
    """Return the documented 60-candidate grid, including the reproduction candidate."""

    return tuple(
        EloConfig(
            k_factor=k_factor,
            home_advantage=home_advantage,
            season_regression=season_regression,
        )
        for k_factor in (16.0, 24.0, 32.0, 40.0, 48.0)
        for home_advantage in (0.0, 15.0, 30.0, 45.0)
        for season_regression in (0.2, 0.35, 0.5)
    )


def evaluate_probabilities(
    matches: Sequence[EloMatch],
    probabilities: Sequence[float | None],
    *,
    result_known_at: datetime | None = None,
) -> MetricReport:
    """Calculate metrics without merging cohort or result-availability policies."""

    if len(matches) != len(probabilities):
        raise ValueError("matches and probabilities must have equal lengths")
    cohort = _require_single_cohort(matches)
    assert cohort is not None
    if result_known_at is not None:
        _require_aware(result_known_at, "result_known_at")
    losses: list[tuple[float, float, float]] = []
    predicted = 0
    missing = Counter[str]()
    selected_results: list[EloResultRevision] = []
    for match, probability in zip(matches, probabilities, strict=True):
        result = (
            match.select_result_as_of(result_known_at)
            if result_known_at is not None
            else match.evaluation_result
        )
        if result is None:
            missing["result_unavailable"] += 1
        else:
            selected_results.append(result)
        if probability is None:
            missing["prediction_unavailable"] += 1
            continue
        if not isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("probabilities must be finite and between zero and one")
        predicted += 1
        if result is None:
            continue
        outcome = result.outcome
        clipped = min(max(probability, LOG_LOSS_EPSILON), 1.0 - LOG_LOSS_EPSILON)
        brier = (probability - outcome) ** 2
        log_loss = -(outcome * log(clipped) + (1.0 - outcome) * log(1.0 - clipped))
        correct = float((probability >= 0.5) == bool(outcome))
        losses.append((brier, log_loss, correct))
    evaluated = len(losses)
    scheduled = len(matches)
    result_available = len(selected_results)
    return MetricReport(
        metric_version=METRIC_VERSION,
        cohort=cohort,
        evaluation_result_known_at=result_known_at,
        scheduled=scheduled,
        predicted=predicted,
        result_available=result_available,
        evaluated=evaluated,
        missing_predictions=scheduled - predicted,
        missing_results=scheduled - result_available,
        coverage=evaluated / scheduled if scheduled else 0.0,
        brier_score=(sum(item[0] for item in losses) / evaluated if evaluated else None),
        log_loss=(sum(item[1] for item in losses) / evaluated if evaluated else None),
        accuracy=(sum(item[2] for item in losses) / evaluated if evaluated else None),
        missing_reasons=MappingProxyType(dict(sorted(missing.items()))),
        result_revision_ids=tuple(result.revision_id for result in selected_results),
        result_raw_snapshot_sha256=tuple(result.raw_snapshot_sha256 for result in selected_results),
    )


def run_baseline_experiment(
    matches: Sequence[EloMatch],
    split: TemporalSplit,
    candidates: Sequence[EloConfig] | None = None,
) -> BaselineExperimentReport:
    """Select on validation only, then run one frozen walk-forward test."""

    ordered = _ordered_unique(matches)
    cohort = _require_single_cohort(ordered)
    assert cohort is not None
    train, validation, test = _partition(ordered, split)
    if not train or not validation or not test:
        raise ValueError("train, validation, and test periods must each contain matches")
    if len(train) + len(validation) + len(test) != len(ordered):
        raise ValueError("all inputs must fall inside the declared split boundaries")
    candidate_set = tuple(candidates or default_candidate_grid())
    if not candidate_set:
        raise ValueError("at least one Elo candidate is required")
    if len({config.config_id for config in candidate_set}) != len(candidate_set):
        raise ValueError("candidate configurations must be unique")

    validations = tuple(
        CandidateValidation(
            config=config,
            metrics=_evaluate_elo(
                config,
                train + validation,
                validation,
                result_known_at=split.test_start,
            ),
        )
        for config in candidate_set
    )
    eligible = [item for item in validations if item.metrics.brier_score is not None]
    if not eligible:
        raise ValueError("validation period has no final results known before test")
    selected = min(
        eligible,
        key=lambda item: (
            item.metrics.brier_score,
            item.metrics.log_loss,
            item.config.config_id,
        ),
    ).config
    elo_test = _evaluate_elo(
        selected,
        train + validation + test,
        test,
        result_known_at=split.test_end,
    )

    home = HomeWinRateBaseline()
    home.fit(train, known_at=split.train_end)
    home_validation = evaluate_probabilities(
        validation,
        [home.predict(match) for match in validation],
        result_known_at=split.test_start,
    )
    home_test = evaluate_probabilities(
        test,
        [home.predict(match) for match in test],
        result_known_at=split.test_end,
    )

    return BaselineExperimentReport(
        cohort=cohort,
        split=split,
        input_manifest=_build_manifest(ordered),
        selected_config=selected,
        candidate_validation=validations,
        elo_test=elo_test,
        home_win_probability=home.probability,
        home_training_sample_size=home.sample_size,
        home_training_wins=home.home_wins,
        home_training_result_revision_ids=home.result_revision_ids,
        home_validation=home_validation,
        home_test=home_test,
        selection_data_ends_before=split.test_start,
        test_started_at=split.test_start,
    )


def run_baseline_suite(
    matches: Iterable[EloMatch],
    split: TemporalSplit,
    candidates: Sequence[EloConfig] | None = None,
) -> BaselineSuiteReport:
    """Run every full experiment cohort independently."""

    grouped: defaultdict[CohortKey, list[EloMatch]] = defaultdict(list)
    for match in matches:
        grouped[CohortKey.from_match(match)].append(match)
    reports = tuple(
        run_baseline_experiment(grouped[cohort], split, candidates) for cohort in sorted(grouped)
    )
    return BaselineSuiteReport(reports)


def _evaluate_elo(
    config: EloConfig,
    timeline: Sequence[EloMatch],
    evaluation: Sequence[EloMatch],
    *,
    result_known_at: datetime,
) -> MetricReport:
    probabilities_by_match = walk_forward_probabilities(timeline, config)
    probabilities = [probabilities_by_match[match.match_id] for match in evaluation]
    return evaluate_probabilities(
        evaluation,
        probabilities,
        result_known_at=result_known_at,
    )


def walk_forward_probabilities(
    matches: Sequence[EloMatch],
    config: EloConfig,
) -> dict[str, float]:
    """Return cutoff-ordered probabilities using only then-known final results."""

    return {
        match_id: prediction.home_win_probability
        for match_id, prediction in walk_forward_predictions(matches, config).items()
    }


def walk_forward_predictions(
    matches: Sequence[EloMatch],
    config: EloConfig,
) -> dict[str, EloPrediction]:
    """Return full cutoff-ordered Elo artifacts with immutable input identity."""

    matches = _ordered_unique(matches)
    cohort = _require_single_cohort(matches)
    assert cohort is not None
    predictor = EloPredictor(cohort.division, config)
    history: list[EloMatch] = []
    selected: dict[str, EloResultRevision] = {}
    predictions: dict[str, EloPrediction] = {}
    for target in matches:
        desired = {
            previous.match_id: result
            for previous in history
            if (result := previous.select_result_as_of(target.prediction_cutoff_at)) is not None
        }
        if desired != selected:
            changed = {
                match_id
                for match_id in set(desired) | set(selected)
                if desired.get(match_id) != selected.get(match_id)
            }
            previous = history[-1] if history else None
            immediate = (
                len(changed) == 1
                and previous is not None
                and previous.match_id in changed
                and previous.match_id not in selected
                and predictor.current_match_id == previous.match_id
            )
            if immediate:
                assert previous is not None
                predictor.update_observed_result(
                    previous,
                    desired[previous.match_id],
                    known_at=target.prediction_cutoff_at,
                )
            else:
                predictor = _replay_as_of(
                    cohort.division,
                    config,
                    history,
                    desired,
                    known_at=target.prediction_cutoff_at,
                )
            selected = desired
        predictor.transition(target)
        predictions[target.match_id] = predictor.predict(target)
        history.append(target)
    return predictions


def _replay_as_of(
    division: Division,
    config: EloConfig,
    history: Sequence[EloMatch],
    selected: dict[str, EloResultRevision],
    *,
    known_at: datetime,
) -> EloPredictor:
    predictor = EloPredictor(division, config)
    for match in history:
        predictor.transition(match)
        result = selected.get(match.match_id)
        if result is not None:
            predictor.update_observed_result(match, result, known_at=known_at)
    return predictor


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


def _ordered_unique(matches: Sequence[EloMatch]) -> list[EloMatch]:
    if len({match.match_id for match in matches}) != len(matches):
        raise ValueError("match_id must be unique within an experiment")
    return sorted(matches, key=lambda match: match.order_key)


def _require_single_cohort(
    matches: Sequence[EloMatch],
    *,
    allow_empty: bool = False,
) -> CohortKey | None:
    cohorts = {CohortKey.from_match(match) for match in matches}
    if not cohorts:
        if allow_empty:
            return None
        raise ValueError("metrics and experiments require matches")
    if len(cohorts) != 1:
        raise ValueError("experiment cohort dimensions must remain separate")
    return next(iter(cohorts))


def _build_manifest(matches: Sequence[EloMatch]) -> InputManifest:
    entries = tuple(
        InputManifestEntry(position=position, match=match) for position, match in enumerate(matches)
    )
    documents = [entry.to_dict() for entry in entries]
    encoded = json.dumps(
        documents,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return InputManifest(entries=entries, sha256=hashlib.sha256(encoded).hexdigest())


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
