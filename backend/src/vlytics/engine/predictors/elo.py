"""Deterministic, point-in-time Elo home-win baseline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any

from vlytics.engine.features import AvailabilityPolicy

DEFAULT_ELO_VERSION = "elo-home-win-v2"
DEFAULT_RANDOM_SEED = 0
ELO_INPUT_VERSION = "elo-match-v2"


class Division(StrEnum):
    MEN = "men"
    WOMEN = "women"


class TimingEligibility(StrEnum):
    """Whether a prediction belongs to a fair timing cohort."""

    ON_TIME = "on_time"
    RECONSTRUCTED = "reconstructed"


class ResultFinality(StrEnum):
    FINAL = "final"
    PROVISIONAL = "provisional"
    VOID = "void"


class ResultFinalityPolicy(StrEnum):
    FINAL_ONLY = "final_only"


class RatingSource(StrEnum):
    CURRENT_SEASON = "current_season"
    VERIFIED_FRANCHISE_CARRYOVER = "verified_franchise_carryover"
    INITIAL = "initial"


@dataclass(frozen=True)
class FranchiseIdentity:
    """A reviewed franchise assignment supplied by the mirror identity layer."""

    franchise_id: str
    mapping_version: str
    evidence: str

    def __post_init__(self) -> None:
        if not self.franchise_id.strip():
            raise ValueError("franchise_id must not be blank")
        if not self.mapping_version.strip() or not self.evidence.strip():
            raise ValueError("franchise carryover requires mapping version and evidence")


@dataclass(frozen=True)
class EloResultRevision:
    """One observed result revision, including when the match became final."""

    match_id: str
    revision_id: str
    revision: int
    observed_at: datetime
    finalized_at: datetime
    raw_snapshot_sha256: str
    finality: ResultFinality
    home_sets: int
    away_sets: int

    def __post_init__(self) -> None:
        if not self.match_id.strip() or not self.revision_id.strip():
            raise ValueError("result match_id and revision_id must not be blank")
        _require_aware(self.observed_at, "result observed_at")
        _require_aware(self.finalized_at, "result finalized_at")
        if self.observed_at < self.finalized_at:
            raise ValueError("result observed_at cannot precede finalized_at")
        if self.revision < 1:
            raise ValueError("result revision must be positive")
        _require_sha256(self.raw_snapshot_sha256, "result raw_snapshot_sha256")
        object.__setattr__(self, "finality", ResultFinality(self.finality))
        if self.home_sets < 0 or self.away_sets < 0:
            raise ValueError("set wins must be non-negative")
        if self.finality is ResultFinality.FINAL:
            if self.home_sets == self.away_sets:
                raise ValueError("a final result cannot be tied")
            if max(self.home_sets, self.away_sets) != 3:
                raise ValueError("a completed volleyball match must have a three-set winner")

    @property
    def outcome(self) -> float:
        if self.finality is not ResultFinality.FINAL:
            raise ValueError("only final result revisions have an Elo outcome")
        return float(self.home_sets > self.away_sets)

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "away_sets": self.away_sets,
            "finality": self.finality.value,
            "finalized_at": self.finalized_at.isoformat(),
            "home_sets": self.home_sets,
            "observed_at": self.observed_at.isoformat(),
            "raw_snapshot_sha256": self.raw_snapshot_sha256,
            "revision": self.revision,
            "revision_id": self.revision_id,
        }


@dataclass(frozen=True)
class EloConfig:
    """Versioned parameters for one deterministic Elo variant."""

    k_factor: float
    home_advantage: float
    season_regression: float
    initial_rating: float = 1500.0
    set_margin_weights: tuple[tuple[int, float], ...] = (
        (1, 0.75),
        (2, 1.0),
        (3, 1.25),
    )
    model_version: str = DEFAULT_ELO_VERSION
    random_seed: int = DEFAULT_RANDOM_SEED

    def __post_init__(self) -> None:
        for field_name in (
            "k_factor",
            "home_advantage",
            "season_regression",
            "initial_rating",
        ):
            object.__setattr__(self, field_name, float(getattr(self, field_name)))
        numeric = (self.k_factor, self.home_advantage, self.season_regression, self.initial_rating)
        if any(not isfinite(value) for value in numeric):
            raise ValueError("Elo parameters must be finite")
        if self.k_factor <= 0:
            raise ValueError("k_factor must be positive")
        if not 0 <= self.season_regression <= 1:
            raise ValueError("season_regression must be between zero and one")
        if not self.model_version.strip():
            raise ValueError("model_version must not be blank")
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise ValueError("random_seed must be an integer")
        weights = dict(self.set_margin_weights)
        if set(weights) != {1, 2, 3} or len(weights) != len(self.set_margin_weights):
            raise ValueError("set_margin_weights must define margins 1, 2, and 3 exactly once")
        if any(not isfinite(weight) or weight <= 0 for weight in weights.values()):
            raise ValueError("set margin weights must be finite and positive")
        object.__setattr__(
            self,
            "set_margin_weights",
            tuple((margin, float(weight)) for margin, weight in sorted(weights.items())),
        )

    @property
    def config_id(self) -> str:
        return _canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "home_advantage": self.home_advantage,
            "initial_rating": self.initial_rating,
            "k_factor": self.k_factor,
            "model_version": self.model_version,
            "random_seed": self.random_seed,
            "season_regression": self.season_regression,
            "set_margin_weights": [list(item) for item in self.set_margin_weights],
        }


def reproduction_candidate() -> EloConfig:
    """Return the historical-feedback candidate without claiming it is optimal."""

    return EloConfig(k_factor=32.0, home_advantage=15.0, season_regression=0.5)


@dataclass(frozen=True)
class EloMatch:
    """One immutable prediction input with all known result revisions."""

    match_id: str
    schedule_revision_id: str
    schedule_revision: int
    schedule_observed_at: datetime
    schedule_raw_snapshot_sha256: str
    prediction_cutoff_at: datetime
    scheduled_start_at: datetime
    season_id: str
    competition: str
    stage: str
    division: Division
    home_team_id: str
    away_team_id: str
    availability_policy: AvailabilityPolicy
    timing_eligibility: TimingEligibility
    result_finality_policy: ResultFinalityPolicy
    franchise_mapping_version: str
    input_version: str = ELO_INPUT_VERSION
    result_revisions: tuple[EloResultRevision, ...] = ()
    home_franchise: FranchiseIdentity | None = None
    away_franchise: FranchiseIdentity | None = None

    def __post_init__(self) -> None:
        text_fields = (
            self.match_id,
            self.schedule_revision_id,
            self.season_id,
            self.competition,
            self.stage,
            self.home_team_id,
            self.away_team_id,
            self.franchise_mapping_version,
            self.input_version,
        )
        if any(not value.strip() for value in text_fields):
            raise ValueError("Elo match identity and version fields must not be blank")
        if self.schedule_revision < 1:
            raise ValueError("schedule_revision must be positive")
        _require_aware(self.schedule_observed_at, "schedule_observed_at")
        _require_aware(self.prediction_cutoff_at, "prediction_cutoff_at")
        _require_aware(self.scheduled_start_at, "scheduled_start_at")
        _require_sha256(self.schedule_raw_snapshot_sha256, "schedule_raw_snapshot_sha256")
        if self.prediction_cutoff_at > self.scheduled_start_at:
            raise ValueError("prediction cutoff cannot follow the scheduled start")
        if self.home_team_id == self.away_team_id:
            raise ValueError("home and away teams must differ")
        object.__setattr__(self, "division", Division(self.division))
        object.__setattr__(
            self, "availability_policy", AvailabilityPolicy(self.availability_policy)
        )
        object.__setattr__(self, "timing_eligibility", TimingEligibility(self.timing_eligibility))
        object.__setattr__(
            self,
            "result_finality_policy",
            ResultFinalityPolicy(self.result_finality_policy),
        )
        if self.availability_policy is AvailabilityPolicy.HISTORICAL_RECONSTRUCTION:
            if self.timing_eligibility is not TimingEligibility.RECONSTRUCTED:
                raise ValueError("reconstruction inputs require reconstructed timing eligibility")
        else:
            if self.timing_eligibility is not TimingEligibility.ON_TIME:
                raise ValueError("strict and live inputs require on-time eligibility")
            if self.schedule_observed_at > self.prediction_cutoff_at:
                raise ValueError("strict and live schedules must be observed by cutoff")
        revision_ids = {revision.revision_id for revision in self.result_revisions}
        revision_numbers = {revision.revision for revision in self.result_revisions}
        if len(revision_ids) != len(self.result_revisions):
            raise ValueError("result revision IDs must be unique per match")
        if len(revision_numbers) != len(self.result_revisions):
            raise ValueError("result revision numbers must be unique per match")
        for revision in self.result_revisions:
            if revision.match_id != self.match_id:
                raise ValueError("result revision match_id must match its schedule")
            if revision.finalized_at < self.scheduled_start_at:
                raise ValueError("a result cannot be final before the scheduled start")
        for identity in (self.home_franchise, self.away_franchise):
            if identity is not None and identity.mapping_version != self.franchise_mapping_version:
                raise ValueError("franchise identity must use the match mapping version")

    @property
    def order_key(self) -> tuple[datetime, str]:
        return (self.prediction_cutoff_at, self.match_id)

    @property
    def rating_scope(self) -> tuple[str, ...]:
        return (
            self.division.value,
            self.availability_policy.value,
            self.competition,
            self.stage,
            self.timing_eligibility.value,
            self.result_finality_policy.value,
            self.input_version,
            self.franchise_mapping_version,
        )

    @property
    def fingerprint(self) -> str:
        return _canonical_sha256(self.to_manifest_dict())

    @property
    def prediction_fingerprint(self) -> str:
        """Hash fields frozen before the match, excluding later result receipts."""

        document = self.to_manifest_dict()
        del document["result_revisions"]
        return _canonical_sha256(document)

    @property
    def evaluation_result(self) -> EloResultRevision | None:
        latest = max(self.result_revisions, key=_result_order_key, default=None)
        if latest is None or latest.finality is not ResultFinality.FINAL:
            return None
        return latest

    @property
    def outcome(self) -> float | None:
        result = self.evaluation_result
        return result.outcome if result is not None else None

    def select_result_as_of(self, known_at: datetime) -> EloResultRevision | None:
        """Select the latest final revision that this cohort may know at a cutoff."""

        _require_aware(known_at, "known_at")
        candidates = [
            revision for revision in self.result_revisions if revision.finalized_at <= known_at
        ]
        if self.availability_policy is not AvailabilityPolicy.HISTORICAL_RECONSTRUCTION:
            candidates = [revision for revision in candidates if revision.observed_at <= known_at]
        latest = max(candidates, key=_result_order_key, default=None)
        if latest is None or latest.finality is not ResultFinality.FINAL:
            return None
        return latest

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "availability_policy": self.availability_policy.value,
            "away_franchise_id": (
                self.away_franchise.franchise_id if self.away_franchise else None
            ),
            "away_team_id": self.away_team_id,
            "competition": self.competition,
            "division": self.division.value,
            "franchise_mapping_version": self.franchise_mapping_version,
            "home_franchise_id": (
                self.home_franchise.franchise_id if self.home_franchise else None
            ),
            "home_team_id": self.home_team_id,
            "input_version": self.input_version,
            "match_id": self.match_id,
            "prediction_cutoff_at": self.prediction_cutoff_at.isoformat(),
            "result_finality_policy": self.result_finality_policy.value,
            "result_revisions": [
                revision.to_manifest_dict()
                for revision in sorted(self.result_revisions, key=_result_order_key)
            ],
            "schedule_observed_at": self.schedule_observed_at.isoformat(),
            "schedule_raw_snapshot_sha256": self.schedule_raw_snapshot_sha256,
            "schedule_revision": self.schedule_revision,
            "schedule_revision_id": self.schedule_revision_id,
            "scheduled_start_at": self.scheduled_start_at.isoformat(),
            "season_id": self.season_id,
            "stage": self.stage,
            "timing_eligibility": self.timing_eligibility.value,
        }


@dataclass(frozen=True)
class EloPrediction:
    match_id: str
    schedule_revision_id: str
    schedule_raw_snapshot_sha256: str
    prediction_cutoff_at: datetime
    division: Division
    availability_policy: AvailabilityPolicy
    competition: str
    stage: str
    season_id: str
    home_win_probability: float
    home_rating: float
    away_rating: float
    home_rating_source: RatingSource
    away_rating_source: RatingSource
    home_franchise_id: str | None
    away_franchise_id: str | None
    franchise_mapping_version: str
    input_version: str
    model_version: str
    config_id: str
    random_seed: int


@dataclass(frozen=True)
class _PreparedMatch:
    fingerprint: str
    match_id: str
    home_rating: float
    away_rating: float
    home_source: RatingSource
    away_source: RatingSource


class EloPredictor:
    """State machine with explicit transition, pure prediction, and result update."""

    def __init__(self, division: Division, config: EloConfig) -> None:
        self.division = Division(division)
        self.config = config
        self._rating_scope: tuple[str, ...] | None = None
        self._season_id: str | None = None
        self._completed_seasons: set[str] = set()
        self._ratings: dict[str, float] = {}
        self._team_franchises: dict[str, FranchiseIdentity] = {}
        self._carryover_ratings: dict[str, float] = {}
        self._last_order_key: tuple[datetime, str] | None = None
        self._prepared: _PreparedMatch | None = None
        self._applied_results: dict[str, str] = {}

    @property
    def ratings(self) -> MappingProxyType[str, float]:
        return MappingProxyType(dict(self._ratings))

    @property
    def current_match_id(self) -> str | None:
        return self._prepared.match_id if self._prepared else None

    def transition(self, match: EloMatch) -> bool:
        """Idempotently prepare a team's season state for one ordered match."""

        if match.division is not self.division:
            raise ValueError("an EloPredictor cannot mix men and women divisions")
        if self._rating_scope is None:
            self._rating_scope = match.rating_scope
        elif match.rating_scope != self._rating_scope:
            raise ValueError("an EloPredictor cannot mix rating cohorts")
        if (
            self._prepared is not None
            and self._prepared.fingerprint == match.prediction_fingerprint
        ):
            return False
        if self._last_order_key is not None and match.order_key <= self._last_order_key:
            raise ValueError("match transitions must be in strict cutoff order")
        if self._season_id is None:
            self._season_id = match.season_id
        elif match.season_id != self._season_id:
            if match.season_id in self._completed_seasons:
                raise ValueError("matches cannot return to a completed season")
            self._complete_season()
            self._completed_seasons.add(self._season_id)
            self._season_id = match.season_id
        home_rating, home_source = self._ensure_team(match.home_team_id, match.home_franchise)
        away_rating, away_source = self._ensure_team(match.away_team_id, match.away_franchise)
        self._prepared = _PreparedMatch(
            match.prediction_fingerprint,
            match.match_id,
            home_rating,
            away_rating,
            home_source,
            away_source,
        )
        self._last_order_key = match.order_key
        return True

    def predict(self, match: EloMatch) -> EloPrediction:
        """Purely read the prepared pre-match state; repeated calls are identical."""

        prepared = self._require_prepared(match)
        probability = self._probability(prepared.home_rating, prepared.away_rating)
        return EloPrediction(
            match_id=match.match_id,
            schedule_revision_id=match.schedule_revision_id,
            schedule_raw_snapshot_sha256=match.schedule_raw_snapshot_sha256,
            prediction_cutoff_at=match.prediction_cutoff_at,
            division=match.division,
            availability_policy=match.availability_policy,
            competition=match.competition,
            stage=match.stage,
            season_id=match.season_id,
            home_win_probability=probability,
            home_rating=prepared.home_rating,
            away_rating=prepared.away_rating,
            home_rating_source=prepared.home_source,
            away_rating_source=prepared.away_source,
            home_franchise_id=(match.home_franchise.franchise_id if match.home_franchise else None),
            away_franchise_id=(match.away_franchise.franchise_id if match.away_franchise else None),
            franchise_mapping_version=match.franchise_mapping_version,
            input_version=match.input_version,
            model_version=self.config.model_version,
            config_id=self.config.config_id,
            random_seed=self.config.random_seed,
        )

    def update_observed_result(
        self,
        match: EloMatch,
        result: EloResultRevision,
        *,
        known_at: datetime,
    ) -> bool:
        """Idempotently apply the latest result known at a declared cutoff."""

        applied = self._applied_results.get(match.match_id)
        if applied == result.revision_id:
            return False
        if applied is not None:
            raise ValueError("a changed result revision requires deterministic replay")
        prepared = self._require_prepared(match)
        selected = match.select_result_as_of(known_at)
        if selected is None or selected.revision_id != result.revision_id:
            raise ValueError("result revision was not available under the match policy")
        margin = abs(result.home_sets - result.away_sets)
        weight = dict(self.config.set_margin_weights)[margin]
        change = (
            self.config.k_factor
            * weight
            * (result.outcome - self._probability(prepared.home_rating, prepared.away_rating))
        )
        self._ratings[match.home_team_id] = prepared.home_rating + change
        self._ratings[match.away_team_id] = prepared.away_rating - change
        self._applied_results[match.match_id] = result.revision_id
        return True

    def _probability(self, home_rating: float, away_rating: float) -> float:
        return float(
            1.0 / (1.0 + 10.0 ** ((away_rating - home_rating - self.config.home_advantage) / 400.0))
        )

    def _require_prepared(self, match: EloMatch) -> _PreparedMatch:
        if self._prepared is None or self._prepared.fingerprint != match.prediction_fingerprint:
            raise ValueError("transition(match) must prepare the exact input before use")
        return self._prepared

    def _complete_season(self) -> None:
        carryover: dict[str, float] = {}
        for team_id, identity in self._team_franchises.items():
            if identity.franchise_id in carryover:
                raise ValueError("a franchise cannot map to multiple teams in one season")
            carryover[identity.franchise_id] = self._ratings[team_id]
        self._carryover_ratings = carryover
        self._ratings = {}
        self._team_franchises = {}

    def _ensure_team(
        self,
        team_id: str,
        franchise: FranchiseIdentity | None,
    ) -> tuple[float, RatingSource]:
        if team_id in self._ratings:
            known = self._team_franchises.get(team_id)
            if known != franchise:
                raise ValueError("a team's verified franchise assignment changed within a season")
            return self._ratings[team_id], RatingSource.CURRENT_SEASON
        source = RatingSource.INITIAL
        rating = self.config.initial_rating
        if franchise is not None and franchise.franchise_id in self._carryover_ratings:
            old_rating = self._carryover_ratings[franchise.franchise_id]
            rating = self.config.initial_rating + (1.0 - self.config.season_regression) * (
                old_rating - self.config.initial_rating
            )
            source = RatingSource.VERIFIED_FRANCHISE_CARRYOVER
        if franchise is not None:
            if any(
                existing.franchise_id == franchise.franchise_id
                for existing in self._team_franchises.values()
            ):
                raise ValueError("a franchise cannot map to multiple teams in one season")
            self._team_franchises[team_id] = franchise
        self._ratings[team_id] = rating
        return rating, source


def _result_order_key(result: EloResultRevision) -> tuple[int, datetime, str]:
    return (result.revision, result.observed_at, result.revision_id)


def _canonical_sha256(document: Any) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
