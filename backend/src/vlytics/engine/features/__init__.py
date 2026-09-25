"""Versioned, leakage-safe feature computation."""

from vlytics.engine.features.as_of import AsOfFeatureBuilder
from vlytics.engine.features.definitions import (
    FEATURE_DEFINITIONS,
    FEATURE_VERSION,
    VERIFIED_METRIC_SCHEMA_VERSION,
    contract_definitions,
)
from vlytics.engine.features.models import (
    AvailabilityPolicy,
    FeatureLineage,
    FeatureSnapshot,
    FeatureStatus,
    FeatureValue,
    HistoricalMatch,
    LineupStatus,
    MatchScheduleRevision,
    MetricScope,
    MissingReason,
    PlayerFeatures,
    PlayerStatLine,
    ResultRevision,
    RosterRevision,
    SetScore,
    StatLine,
    TargetMatch,
    compute_snapshot_sha256,
)
from vlytics.engine.features.repository import (
    FeatureSnapshotRepository,
    FeatureSnapshotValidationError,
    StoredFeatureSnapshot,
)

__all__ = [
    "FEATURE_DEFINITIONS",
    "FEATURE_VERSION",
    "VERIFIED_METRIC_SCHEMA_VERSION",
    "AsOfFeatureBuilder",
    "AvailabilityPolicy",
    "FeatureLineage",
    "FeatureSnapshot",
    "FeatureSnapshotRepository",
    "FeatureSnapshotValidationError",
    "StoredFeatureSnapshot",
    "FeatureStatus",
    "FeatureValue",
    "HistoricalMatch",
    "LineupStatus",
    "MatchScheduleRevision",
    "MetricScope",
    "MissingReason",
    "PlayerFeatures",
    "PlayerStatLine",
    "ResultRevision",
    "RosterRevision",
    "SetScore",
    "StatLine",
    "TargetMatch",
    "compute_snapshot_sha256",
    "contract_definitions",
]
