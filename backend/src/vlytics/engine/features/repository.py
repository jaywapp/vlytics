"""Persistence boundary for immutable feature snapshots."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from sqlalchemy import Connection, text

from vlytics.engine.features.models import (
    AvailabilityPolicy,
    FeatureLineage,
    FeatureSnapshot,
    FeatureStatus,
    FeatureValue,
    LineupStatus,
    MissingReason,
    PlayerFeatures,
    compute_snapshot_sha256,
)


class FeatureSnapshotValidationError(ValueError):
    """Raised before database access when a snapshot is not contract safe."""


@dataclass(frozen=True)
class StoredFeatureSnapshot:
    id: UUID
    snapshot: FeatureSnapshot


class FeatureSnapshotRepository:
    """Append a computed snapshot to the engine-owned table."""

    def __init__(
        self,
        connection: Connection,
        schema_path: Path | None = None,
    ) -> None:
        self._connection = connection
        contract = schema_path or (
            Path(__file__).resolve().parents[5] / "contracts" / "feature-v1.schema.json"
        )
        schema = json.loads(contract.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self._validator = Draft202012Validator(
            schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )

    def add(self, snapshot: FeatureSnapshot) -> UUID:
        """Persist values and lineage without changing an existing snapshot."""

        document = snapshot.to_dict()
        errors = sorted(self._validator.iter_errors(document), key=lambda error: list(error.path))
        if errors:
            first = errors[0]
            path = ".".join(str(part) for part in first.path) or "$"
            raise FeatureSnapshotValidationError(
                f"feature snapshot schema validation failed at {path}: {first.message}"
            )
        computed_sha256 = compute_snapshot_sha256(snapshot)
        if snapshot.sha256 != computed_sha256:
            raise FeatureSnapshotValidationError(
                "feature snapshot SHA-256 does not match its canonical payload"
            )
        values = {
            "team_features": document["team_features"],
            "matchup_features": document["matchup_features"],
            "players": document["players"],
        }
        row_id = self._connection.execute(
            text(
                """
                INSERT INTO engine.feature_snapshots (
                    match_id, schedule_revision_id, cutoff_at, captured_at,
                    feature_version, availability_policy, lineup_status,
                    values_json, lineage_json, sha256
                ) VALUES (
                    :match_id, :schedule_revision_id, :cutoff_at, :captured_at,
                    :feature_version, :availability_policy, :lineup_status,
                    CAST(:values_json AS jsonb), CAST(:lineage_json AS jsonb), :sha256
                ) RETURNING id
                """
            ),
            {
                "match_id": snapshot.target_match_id,
                "schedule_revision_id": snapshot.schedule_revision_id,
                "cutoff_at": snapshot.cutoff_at,
                "captured_at": snapshot.captured_at,
                "feature_version": snapshot.feature_version,
                "availability_policy": snapshot.availability_policy.value,
                "lineup_status": snapshot.lineup_status.value,
                "values_json": json.dumps(values, sort_keys=True, separators=(",", ":")),
                "lineage_json": json.dumps(
                    document["lineage"], sort_keys=True, separators=(",", ":")
                ),
                "sha256": computed_sha256,
            },
        ).scalar_one()
        return cast(UUID, row_id)

    def freeze(
        self,
        *,
        match_id: UUID,
        schedule_revision_id: UUID,
        cutoff_at: datetime,
        feature_version: str,
        availability_policy: AvailabilityPolicy,
        build: Callable[[], FeatureSnapshot],
    ) -> StoredFeatureSnapshot:
        """Serialize one identity and return the first immutable snapshot after restarts."""

        identity = (
            f"{match_id}:{schedule_revision_id}:{feature_version}:"
            f"{cutoff_at.astimezone(UTC).isoformat()}:{availability_policy.value}"
        )
        self._connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": identity},
        )
        existing = self.find(
            match_id=match_id,
            schedule_revision_id=schedule_revision_id,
            cutoff_at=cutoff_at,
            feature_version=feature_version,
            availability_policy=availability_policy,
        )
        if existing is not None:
            return existing
        snapshot = build()
        if (
            snapshot.target_match_id != str(match_id)
            or snapshot.schedule_revision_id != str(schedule_revision_id)
            or snapshot.cutoff_at != cutoff_at
            or snapshot.feature_version != feature_version
            or snapshot.availability_policy is not availability_policy
        ):
            raise FeatureSnapshotValidationError(
                "built snapshot identity differs from the requested freeze identity"
            )
        return StoredFeatureSnapshot(self.add(snapshot), snapshot)

    def find(
        self,
        *,
        match_id: UUID,
        schedule_revision_id: UUID,
        cutoff_at: datetime,
        feature_version: str,
        availability_policy: AvailabilityPolicy,
    ) -> StoredFeatureSnapshot | None:
        row = (
            self._connection.execute(
                text(
                    """
                    SELECT *
                    FROM engine.feature_snapshots
                    WHERE match_id = :match_id
                      AND schedule_revision_id = :schedule_revision_id
                      AND cutoff_at = :cutoff_at
                      AND feature_version = :feature_version
                      AND availability_policy = :availability_policy
                    """
                ),
                {
                    "match_id": match_id,
                    "schedule_revision_id": schedule_revision_id,
                    "cutoff_at": cutoff_at,
                    "feature_version": feature_version,
                    "availability_policy": availability_policy.value,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        snapshot = _snapshot_from_row(cast(Mapping[str, Any], row))
        if compute_snapshot_sha256(snapshot) != snapshot.sha256:
            raise FeatureSnapshotValidationError("stored feature snapshot digest is invalid")
        return StoredFeatureSnapshot(cast(UUID, row["id"]), snapshot)


def _lineage(document: Mapping[str, Any]) -> FeatureLineage:
    return FeatureLineage(
        included_match_ids=tuple(document.get("included_match_ids", ())),
        schedule_revision_ids=tuple(document.get("schedule_revision_ids", ())),
        result_revision_ids=tuple(document.get("result_revision_ids", ())),
        raw_snapshot_sha256=tuple(document.get("raw_snapshot_sha256", ())),
        roster_revision_ids=tuple(document.get("roster_revision_ids", ())),
        metric_schema_versions=tuple(document.get("metric_schema_versions", ())),
    )


def _feature_value(document: Mapping[str, Any]) -> FeatureValue:
    reason = document.get("missing_reason")
    return FeatureValue(
        definition=str(document["definition"]),
        status=FeatureStatus(str(document["status"])),
        value=_optional_float(document.get("value")),
        numerator=_optional_float(document.get("numerator")),
        denominator=_optional_float(document.get("denominator")),
        sample_size=int(document["sample_size"]),
        missing_reason=MissingReason(str(reason)) if reason is not None else None,
        lineage=_lineage(_mapping(document["lineage"])),
    )


def _snapshot_from_row(row: Mapping[str, Any]) -> FeatureSnapshot:
    values = _mapping(row["values_json"])
    team_documents = _mapping(values["team_features"])
    player_documents = _mapping(values["players"])
    return FeatureSnapshot(
        feature_version=str(row["feature_version"]),
        availability_policy=AvailabilityPolicy(str(row["availability_policy"])),
        target_match_id=str(row["match_id"]),
        schedule_revision_id=str(row["schedule_revision_id"]),
        cutoff_at=cast(datetime, row["cutoff_at"]),
        captured_at=cast(datetime, row["captured_at"]),
        lineup_status=LineupStatus(str(row["lineup_status"])),
        team_features={
            str(side): {
                str(key): _feature_value(_mapping(value))
                for key, value in _mapping(features).items()
            }
            for side, features in team_documents.items()
        },
        matchup_features={
            str(key): _feature_value(_mapping(value))
            for key, value in _mapping(values["matchup_features"]).items()
        },
        players={
            str(player_id): PlayerFeatures(
                team_id=str(_mapping(document)["team_id"]),
                side=str(_mapping(document)["side"]),
                features={
                    str(key): _feature_value(_mapping(value))
                    for key, value in _mapping(_mapping(document)["features"]).items()
                },
            )
            for player_id, document in player_documents.items()
        },
        lineage=_lineage(_mapping(row["lineage_json"])),
        sha256=str(row["sha256"]),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FeatureSnapshotValidationError("stored feature snapshot object is malformed")
    return value


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FeatureSnapshotValidationError("stored feature snapshot number is malformed")
    return float(value)
