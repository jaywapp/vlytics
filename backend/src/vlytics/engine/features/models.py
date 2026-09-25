"""Typed inputs and immutable outputs for point-in-time feature computation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any


class AvailabilityPolicy(StrEnum):
    """Mutually exclusive availability cohorts."""

    HISTORICAL_RECONSTRUCTION = "historical_reconstruction"
    HISTORICAL_POINT_IN_TIME = "historical_point_in_time"
    LIVE_PROSPECTIVE = "live_prospective"


class MetricScope(StrEnum):
    """Whether one stat row describes a match or a current cumulative total."""

    MATCH = "match"
    CUMULATIVE = "cumulative"


class MissingReason(StrEnum):
    """Stable reasons for an unavailable feature value."""

    NO_PRIOR_MATCHES = "no_prior_matches"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    MISSING_INPUT = "missing_input"
    SOURCE_METRIC_UNVERIFIED = "source_metric_unverified"
    CUMULATIVE_VALUE_REJECTED = "cumulative_value_rejected"
    ZERO_DENOMINATOR = "zero_denominator"
    LINEUP_UNAVAILABLE = "lineup_unavailable"


class FeatureStatus(StrEnum):
    AVAILABLE = "available"
    UNKNOWN = "unknown"


class LineupStatus(StrEnum):
    KNOWN = "known"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SetScore:
    """One completed set from the selected result revision."""

    home_points: int
    away_points: int

    def __post_init__(self) -> None:
        if self.home_points < 0 or self.away_points < 0:
            raise ValueError("set points must be non-negative")


@dataclass(frozen=True)
class StatLine:
    """A versioned metric row whose semantic schema and scope are explicit."""

    metric_schema_version: str
    values: Mapping[str, float]
    scope: MetricScope = MetricScope.MATCH

    def __post_init__(self) -> None:
        if not self.metric_schema_version.strip():
            raise ValueError("metric_schema_version must not be blank")
        copied = dict(self.values)
        if any(not isfinite(value) or value < 0 for value in copied.values()):
            raise ValueError("stat values must be finite and non-negative")
        object.__setattr__(self, "values", MappingProxyType(copied))


@dataclass(frozen=True)
class PlayerStatLine:
    """A player stat row tied to the team represented in that historical match."""

    team_id: str
    stats: StatLine

    def __post_init__(self) -> None:
        if not self.team_id.strip():
            raise ValueError("player stat team_id must not be blank")


@dataclass(frozen=True)
class ResultRevision:
    """A complete match result revision and its revision-aligned observations."""

    id: str
    revision: int
    observed_at: datetime
    raw_snapshot_sha256: str
    home_sets: int
    away_sets: int
    sets: tuple[SetScore, ...]
    finality: str = "final"
    team_stats: Mapping[str, StatLine] | None = None
    player_stats: Mapping[str, PlayerStatLine] | None = None
    roster_player_ids: Mapping[str, tuple[str, ...]] | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.home_sets < 0 or self.away_sets < 0:
            raise ValueError("set wins must be non-negative")
        _require_sha256(self.raw_snapshot_sha256)
        object.__setattr__(
            self,
            "team_stats",
            MappingProxyType(dict(self.team_stats or {})),
        )
        object.__setattr__(
            self,
            "player_stats",
            MappingProxyType(dict(self.player_stats or {})),
        )
        roster_copy = {
            team_id: tuple(dict.fromkeys(player_ids))
            for team_id, player_ids in (self.roster_player_ids or {}).items()
        }
        object.__setattr__(self, "roster_player_ids", MappingProxyType(roster_copy))


@dataclass(frozen=True)
class MatchScheduleRevision:
    """One observed schedule revision for a historical match."""

    id: str
    revision: int
    observed_at: datetime
    raw_snapshot_sha256: str
    scheduled_start_at: datetime
    ended_at: datetime | None
    home_team_id: str
    away_team_id: str

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.scheduled_start_at, "scheduled_start_at")
        if self.ended_at is not None:
            _require_aware(self.ended_at, "ended_at")
            if self.ended_at < self.scheduled_start_at:
                raise ValueError("ended_at must not precede scheduled_start_at")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.home_team_id == self.away_team_id:
            raise ValueError("home and away teams must differ")
        _require_sha256(self.raw_snapshot_sha256)


@dataclass(frozen=True)
class HistoricalMatch:
    """A prior match whose schedule and result revisions are selected as-of."""

    id: str
    season_id: str
    competition_id: str
    schedule_revisions: tuple[MatchScheduleRevision, ...]
    result_revisions: tuple[ResultRevision, ...]

    def __post_init__(self) -> None:
        if not self.schedule_revisions:
            raise ValueError("historical matches require at least one schedule revision")


@dataclass(frozen=True)
class TargetMatch:
    """The selected target schedule revision for which features are frozen."""

    id: str
    schedule_revision_id: str
    season_id: str
    competition_id: str
    scheduled_start_at: datetime
    schedule_observed_at: datetime
    home_team_id: str
    away_team_id: str

    def __post_init__(self) -> None:
        _require_aware(self.scheduled_start_at, "scheduled_start_at")
        _require_aware(self.schedule_observed_at, "schedule_observed_at")
        if self.home_team_id == self.away_team_id:
            raise ValueError("home and away teams must differ")


@dataclass(frozen=True)
class RosterRevision:
    """One atomic team roster batch observed for the target match."""

    id: str
    target_match_id: str
    team_id: str
    revision: int
    observed_at: datetime
    raw_snapshot_sha256: str
    player_ids: tuple[str, ...]
    complete: bool

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if len(self.player_ids) != len(set(self.player_ids)):
            raise ValueError("roster batches cannot contain duplicate players")
        _require_sha256(self.raw_snapshot_sha256)


@dataclass(frozen=True)
class FeatureLineage:
    included_match_ids: tuple[str, ...] = ()
    schedule_revision_ids: tuple[str, ...] = ()
    result_revision_ids: tuple[str, ...] = ()
    raw_snapshot_sha256: tuple[str, ...] = ()
    roster_revision_ids: tuple[str, ...] = ()
    metric_schema_versions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "included_match_ids": list(self.included_match_ids),
            "schedule_revision_ids": list(self.schedule_revision_ids),
            "result_revision_ids": list(self.result_revision_ids),
            "raw_snapshot_sha256": list(self.raw_snapshot_sha256),
            "roster_revision_ids": list(self.roster_revision_ids),
            "metric_schema_versions": list(self.metric_schema_versions),
        }


@dataclass(frozen=True)
class FeatureValue:
    definition: str
    status: FeatureStatus
    value: float | None
    numerator: float | None
    denominator: float | None
    sample_size: int
    missing_reason: MissingReason | None
    lineage: FeatureLineage

    def __post_init__(self) -> None:
        if self.sample_size < 0:
            raise ValueError("sample_size must be non-negative")
        if self.status is FeatureStatus.AVAILABLE:
            if self.value is None or self.missing_reason is not None:
                raise ValueError("available features require a value and no missing reason")
        elif self.value is not None or self.missing_reason is None:
            raise ValueError("unknown features require null value and a missing reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition": self.definition,
            "status": self.status.value,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "sample_size": self.sample_size,
            "missing_reason": self.missing_reason.value if self.missing_reason else None,
            "lineage": self.lineage.to_dict(),
        }


@dataclass(frozen=True)
class PlayerFeatures:
    team_id: str
    side: str
    features: Mapping[str, FeatureValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "side": self.side,
            "features": {key: value.to_dict() for key, value in sorted(self.features.items())},
        }


@dataclass(frozen=True)
class FeatureSnapshot:
    feature_version: str
    availability_policy: AvailabilityPolicy
    target_match_id: str
    schedule_revision_id: str
    cutoff_at: datetime
    captured_at: datetime
    lineup_status: LineupStatus
    team_features: Mapping[str, Mapping[str, FeatureValue]]
    matchup_features: Mapping[str, FeatureValue]
    players: Mapping[str, PlayerFeatures]
    lineage: FeatureLineage
    sha256: str

    def __post_init__(self) -> None:
        _require_aware(self.cutoff_at, "cutoff_at")
        _require_aware(self.captured_at, "captured_at")
        teams = {
            side: MappingProxyType(dict(features)) for side, features in self.team_features.items()
        }
        object.__setattr__(self, "team_features", MappingProxyType(teams))
        object.__setattr__(
            self,
            "matchup_features",
            MappingProxyType(dict(self.matchup_features)),
        )
        object.__setattr__(self, "players", MappingProxyType(dict(self.players)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_version": self.feature_version,
            "availability_policy": self.availability_policy.value,
            "target_match_id": self.target_match_id,
            "schedule_revision_id": self.schedule_revision_id,
            "cutoff_at": self.cutoff_at.astimezone(UTC).isoformat(),
            "captured_at": self.captured_at.astimezone(UTC).isoformat(),
            "lineup_status": self.lineup_status.value,
            "team_features": {
                side: {key: value.to_dict() for key, value in sorted(features.items())}
                for side, features in sorted(self.team_features.items())
            },
            "matchup_features": {
                key: value.to_dict() for key, value in sorted(self.matchup_features.items())
            },
            "players": {key: value.to_dict() for key, value in sorted(self.players.items())},
            "lineage": self.lineage.to_dict(),
            "sha256": self.sha256,
        }


def compute_snapshot_sha256(snapshot: FeatureSnapshot) -> str:
    """Return the canonical digest over every snapshot field except the digest itself."""

    document = snapshot.to_dict()
    del document["sha256"]
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("raw_snapshot_sha256 must be a lowercase SHA-256 hex digest")
