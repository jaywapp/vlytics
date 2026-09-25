"""Prediction event and rebuildable projection repositories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Connection, text

from vlytics.engine.providers.models import canonical_json_bytes, sha256_bytes


class PredictionConflictError(RuntimeError):
    """A representative prediction already exists with a different immutable body."""


@dataclass(frozen=True)
class StoredPrediction:
    id: UUID
    sha256: str
    created: bool


class PredictionRepository:
    """Insert one representative successful prediction per snapshot, variant, and stage."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def find(
        self,
        *,
        snapshot_id: UUID,
        variant_id: UUID,
        stage: str,
    ) -> StoredPrediction | None:
        row = self._connection.execute(
            text(
                """
                SELECT id, sha256
                FROM engine.predictions
                WHERE snapshot_id = :snapshot_id
                  AND variant_id = :variant_id
                  AND stage = :stage
                """
            ),
            {"snapshot_id": snapshot_id, "variant_id": variant_id, "stage": stage},
        ).one_or_none()
        if row is None:
            return None
        return StoredPrediction(cast(UUID, row[0]), str(row[1]), False)

    def add(
        self,
        *,
        match_id: UUID,
        schedule_revision_id: UUID,
        snapshot_id: UUID,
        variant_id: UUID,
        stage: str,
        input_cutoff_at: datetime,
        started_at: datetime,
        generated_at: datetime,
        resolved_model_id: str,
        output: Mapping[str, Any],
    ) -> StoredPrediction:
        encoded = canonical_json_bytes(output)
        digest = sha256_bytes(encoded)
        prediction_id = self._connection.execute(
            text(
                """
                INSERT INTO engine.predictions (
                    match_id, schedule_revision_id, snapshot_id, variant_id, stage,
                    input_cutoff_at, started_at, generated_at, resolved_model_id,
                    output_json, sha256
                ) VALUES (
                    :match_id, :schedule_revision_id, :snapshot_id, :variant_id, :stage,
                    :input_cutoff_at, :started_at, :generated_at, :resolved_model_id,
                    CAST(:output_json AS jsonb), :sha256
                )
                ON CONFLICT (snapshot_id, variant_id, stage) DO NOTHING
                RETURNING id
                """
            ),
            {
                "match_id": match_id,
                "schedule_revision_id": schedule_revision_id,
                "snapshot_id": snapshot_id,
                "variant_id": variant_id,
                "stage": stage,
                "input_cutoff_at": input_cutoff_at,
                "started_at": started_at,
                "generated_at": generated_at,
                "resolved_model_id": resolved_model_id,
                "output_json": encoded.decode("utf-8"),
                "sha256": digest,
            },
        ).scalar_one_or_none()
        if prediction_id is not None:
            return StoredPrediction(cast(UUID, prediction_id), digest, True)
        existing = self.find(snapshot_id=snapshot_id, variant_id=variant_id, stage=stage)
        if existing is None:
            raise RuntimeError("prediction conflict did not return an existing row")
        if existing.sha256 != digest:
            raise PredictionConflictError(
                "representative prediction already has a different immutable hash"
            )
        return existing


class PredictionEventRepository:
    """Append state changes while leaving prediction rows untouched."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(
        self,
        *,
        prediction_id: UUID,
        event_type: str,
        reason: str,
        occurred_at: datetime,
        observed_at: datetime,
        superseding_prediction_id: UUID | None = None,
        schedule_revision_id: UUID | None = None,
    ) -> UUID:
        """Append a published, voided, superseded, or late-rejected event."""

        event_id = self._connection.execute(
            text(
                """
                INSERT INTO engine.prediction_events (
                    prediction_id, event_type, reason, occurred_at, observed_at,
                    superseding_prediction_id, schedule_revision_id
                ) VALUES (
                    :prediction_id, :event_type, :reason, :occurred_at, :observed_at,
                    :superseding_prediction_id, :schedule_revision_id
                )
                RETURNING id
                """
            ),
            {
                "prediction_id": prediction_id,
                "event_type": event_type,
                "reason": reason,
                "occurred_at": occurred_at,
                "observed_at": observed_at,
                "superseding_prediction_id": superseding_prediction_id,
                "schedule_revision_id": schedule_revision_id,
            },
        ).scalar_one()
        return cast(UUID, event_id)

    def add_once(
        self,
        *,
        prediction_id: UUID,
        event_type: str,
        reason: str,
        occurred_at: datetime,
        observed_at: datetime,
        superseding_prediction_id: UUID | None = None,
        schedule_revision_id: UUID | None = None,
    ) -> UUID:
        """Append an idempotent lifecycle event for replay-safe scheduling."""

        identity = (
            f"{prediction_id}:{event_type}:{schedule_revision_id}:"
            f"{superseding_prediction_id}:{reason}"
        )
        self._connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": identity},
        )
        existing = self._connection.execute(
            text(
                """
                SELECT id
                FROM engine.prediction_events
                WHERE prediction_id = :prediction_id
                  AND event_type = :event_type
                  AND reason = :reason
                  AND schedule_revision_id IS NOT DISTINCT FROM :schedule_revision_id
                  AND superseding_prediction_id
                      IS NOT DISTINCT FROM :superseding_prediction_id
                ORDER BY created_at
                LIMIT 1
                """
            ),
            {
                "prediction_id": prediction_id,
                "event_type": event_type,
                "reason": reason,
                "schedule_revision_id": schedule_revision_id,
                "superseding_prediction_id": superseding_prediction_id,
            },
        ).scalar_one_or_none()
        if existing is not None:
            return cast(UUID, existing)
        return self.add(
            prediction_id=prediction_id,
            event_type=event_type,
            reason=reason,
            occurred_at=occurred_at,
            observed_at=observed_at,
            superseding_prediction_id=superseding_prediction_id,
            schedule_revision_id=schedule_revision_id,
        )

    def void_schedule_revision(
        self,
        *,
        schedule_revision_id: UUID,
        reason: str,
        occurred_at: datetime,
        observed_at: datetime,
    ) -> int:
        prediction_ids = tuple(
            cast(UUID, value)
            for value in self._connection.execute(
                text(
                    """
                    SELECT id
                    FROM engine.predictions
                    WHERE schedule_revision_id = :schedule_revision_id
                    """
                ),
                {"schedule_revision_id": schedule_revision_id},
            ).scalars()
        )
        projection = PredictionProjectionRepository(self._connection)
        for prediction_id in prediction_ids:
            event_id = self.add_once(
                prediction_id=prediction_id,
                event_type="voided",
                reason=reason,
                occurred_at=occurred_at,
                observed_at=observed_at,
                schedule_revision_id=schedule_revision_id,
            )
            projection.apply_event(
                prediction_id=prediction_id,
                event_id=event_id,
                current_status="voided",
                refreshed_at=observed_at,
            )
        return len(prediction_ids)

    def void_match_predictions(
        self,
        *,
        match_id: UUID,
        reason: str,
        occurred_at: datetime,
        observed_at: datetime,
        except_schedule_revision_id: UUID | None = None,
    ) -> int:
        """Void predictions from every affected revision of one match."""

        rows = tuple(
            self._connection.execute(
                text(
                    """
                    SELECT id, schedule_revision_id
                    FROM engine.predictions
                    WHERE match_id = :match_id
                      AND (
                          CAST(:except_schedule_revision_id AS uuid) IS NULL
                          OR schedule_revision_id <> CAST(:except_schedule_revision_id AS uuid)
                      )
                    """
                ),
                {
                    "match_id": match_id,
                    "except_schedule_revision_id": except_schedule_revision_id,
                },
            ).all()
        )
        projection = PredictionProjectionRepository(self._connection)
        for prediction_id, schedule_revision_id in rows:
            event_id = self.add_once(
                prediction_id=cast(UUID, prediction_id),
                event_type="voided",
                reason=reason,
                occurred_at=occurred_at,
                observed_at=observed_at,
                schedule_revision_id=cast(UUID, schedule_revision_id),
            )
            projection.apply_event(
                prediction_id=cast(UUID, prediction_id),
                event_id=event_id,
                current_status="voided",
                refreshed_at=observed_at,
            )
        return len(rows)

    def reject_started_match_predictions(
        self,
        *,
        schedule_revision_id: UUID,
        actual_start_at: datetime,
        observed_at: datetime,
    ) -> int:
        prediction_ids = tuple(
            cast(UUID, value)
            for value in self._connection.execute(
                text(
                    """
                    SELECT id
                    FROM engine.predictions
                    WHERE schedule_revision_id = :schedule_revision_id
                      AND generated_at >= :actual_start_at
                    """
                ),
                {
                    "schedule_revision_id": schedule_revision_id,
                    "actual_start_at": actual_start_at,
                },
            ).scalars()
        )
        projection = PredictionProjectionRepository(self._connection)
        for prediction_id in prediction_ids:
            event_id = self.add_once(
                prediction_id=prediction_id,
                event_type="late_rejected",
                reason="completed_at_or_after_actual_start",
                occurred_at=actual_start_at,
                observed_at=observed_at,
                schedule_revision_id=schedule_revision_id,
            )
            projection.apply_event(
                prediction_id=prediction_id,
                event_id=event_id,
                current_status="late_rejected",
                refreshed_at=observed_at,
            )
        return len(prediction_ids)

    def reject_started_match(
        self,
        *,
        match_id: UUID,
        actual_start_at: datetime,
        observed_at: datetime,
    ) -> int:
        """Late-reject every prediction revision completed at or after actual start."""

        rows = tuple(
            self._connection.execute(
                text(
                    """
                    SELECT id, schedule_revision_id
                    FROM engine.predictions
                    WHERE match_id = :match_id
                      AND generated_at >= :actual_start_at
                    """
                ),
                {"match_id": match_id, "actual_start_at": actual_start_at},
            ).all()
        )
        projection = PredictionProjectionRepository(self._connection)
        for prediction_id, schedule_revision_id in rows:
            event_id = self.add_once(
                prediction_id=cast(UUID, prediction_id),
                event_type="late_rejected",
                reason="completed_at_or_after_actual_start",
                occurred_at=actual_start_at,
                observed_at=observed_at,
                schedule_revision_id=cast(UUID, schedule_revision_id),
            )
            projection.apply_event(
                prediction_id=cast(UUID, prediction_id),
                event_id=event_id,
                current_status="late_rejected",
                refreshed_at=observed_at,
            )
        return len(rows)


class PredictionProjectionRepository:
    """Maintain the disposable current-status projection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def apply_event(
        self,
        *,
        prediction_id: UUID,
        event_id: UUID,
        current_status: str,
        refreshed_at: datetime,
    ) -> None:
        """Insert or replace a projection row from an immutable event."""

        self._connection.execute(
            text(
                """
                INSERT INTO engine.prediction_status_projection (
                    prediction_id, latest_event_id, current_status, refreshed_at
                ) VALUES (
                    :prediction_id, :event_id, :current_status, :refreshed_at
                )
                ON CONFLICT (prediction_id) DO UPDATE SET
                    latest_event_id = EXCLUDED.latest_event_id,
                    current_status = EXCLUDED.current_status,
                    refreshed_at = EXCLUDED.refreshed_at
                """
            ),
            {
                "prediction_id": prediction_id,
                "event_id": event_id,
                "current_status": current_status,
                "refreshed_at": refreshed_at,
            },
        )
