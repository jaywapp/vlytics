"""Persistence contracts for immutable Market snapshots, comparisons, and settlements."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from vlytics.engine.market.models import (
    AvailabilityStatus,
    EvaluationEligibility,
    MarketAvailability,
    MarketContractError,
    MarketEvaluation,
    MarketSettlement,
    MarketSnapshot,
)
from vlytics.engine.market.validation import SourceEventResolver, validate_market_snapshot_v1


class MarketSnapshotRepository:
    """Persist mapped snapshots and select their exact as-of availability state."""

    def __init__(
        self,
        connection: Connection,
        schema: dict[str, Any],
        source_event_resolver: SourceEventResolver,
    ) -> None:
        self._connection = connection
        self._schema = schema
        self._source_event_resolver = source_event_resolver

    def add(self, snapshot: MarketSnapshot) -> UUID:
        mapped_match = self._source_event_resolver(snapshot.source, snapshot.source_event_id)
        if mapped_match != snapshot.match_id:
            raise MarketContractError("source_event mapping does not match snapshot match_id")
        self._connection.execute(
            text(
                """
                INSERT INTO market.source_event_mappings (source, source_event_id, match_id)
                VALUES (:source, :source_event_id, :match_id)
                ON CONFLICT (source, source_event_id) DO NOTHING
                """
            ),
            {
                "source": snapshot.source,
                "source_event_id": snapshot.source_event_id,
                "match_id": UUID(snapshot.match_id),
            },
        )
        snapshot_id = cast(
            UUID,
            self._connection.execute(
                text(
                    """
                    INSERT INTO market.market_snapshots (
                        id, match_id, source, source_event_id, quoted_at, observed_at,
                        received_at, contract_version, markets_json, sha256
                    ) VALUES (
                        :id, :match_id, :source, :source_event_id, :quoted_at, :observed_at,
                        :received_at, :contract_version, CAST(:markets_json AS jsonb), :sha256
                    )
                    RETURNING id
                    """
                ),
                {
                    "id": UUID(snapshot.snapshot_id),
                    "match_id": UUID(snapshot.match_id),
                    "source": snapshot.source,
                    "source_event_id": snapshot.source_event_id,
                    "quoted_at": snapshot.quoted_at,
                    "observed_at": snapshot.observed_at,
                    "received_at": snapshot.received_at,
                    "contract_version": snapshot.contract_version,
                    "markets_json": json.dumps(
                        {"markets": [line.to_dict() for line in snapshot.markets]},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "sha256": snapshot.sha256,
                },
            ).scalar_one(),
        )
        for line in snapshot.markets:
            self._connection.execute(
                text(
                    """
                    INSERT INTO market.market_snapshot_lines (
                        market_snapshot_id, line_id, line_json
                    ) VALUES (:market_snapshot_id, :line_id, CAST(:line_json AS jsonb))
                    """
                ),
                {
                    "market_snapshot_id": snapshot_id,
                    "line_id": line.line_id,
                    "line_json": json.dumps(line.to_dict(), sort_keys=True, separators=(",", ":")),
                },
            )
        return snapshot_id

    def availability_as_of(
        self,
        *,
        match_id: UUID,
        cutoff_at: datetime,
        max_age: timedelta,
    ) -> MarketAvailability:
        if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
            raise MarketContractError("cutoff_at must be timezone-aware")
        if max_age < timedelta(0):
            raise MarketContractError("max_age must be non-negative")
        row = (
            self._connection.execute(
                text(
                    """
                    SELECT id, match_id, source, source_event_id, quoted_at, observed_at,
                           received_at, contract_version, markets_json, sha256
                    FROM market.market_snapshots
                    WHERE match_id = :match_id
                      AND quoted_at <= :cutoff_at
                      AND observed_at <= :cutoff_at
                      AND received_at <= :cutoff_at
                    ORDER BY quoted_at DESC, observed_at DESC, received_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"match_id": match_id, "cutoff_at": cutoff_at},
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            snapshot = self._snapshot_from_row(row)
            if cutoff_at - snapshot.quoted_at > max_age:
                return MarketAvailability(
                    status=AvailabilityStatus.STALE,
                    reason="latest_quote_exceeds_max_age",
                )
            return MarketAvailability(
                status=AvailabilityStatus.AVAILABLE,
                reason="latest_quote_available_as_of_cutoff",
                snapshot=snapshot,
            )
        late_exists = self._connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM market.market_snapshots
                    WHERE match_id = :match_id
                      AND quoted_at <= :cutoff_at
                      AND (observed_at > :cutoff_at OR received_at > :cutoff_at)
                )
                """
            ),
            {"match_id": match_id, "cutoff_at": cutoff_at},
        ).scalar_one()
        if late_exists:
            return MarketAvailability(
                status=AvailabilityStatus.LATE,
                reason="late_quote_received_after_cutoff",
            )
        return MarketAvailability(
            status=AvailabilityStatus.MISSING,
            reason="no_market_snapshot_as_of_cutoff",
        )

    def _snapshot_from_row(self, row: RowMapping) -> MarketSnapshot:
        document = {
            "schema_version": "market-v1",
            "snapshot_id": str(row["id"]),
            "match_id": str(row["match_id"]),
            "source": row["source"],
            "source_event_id": row["source_event_id"],
            "quoted_at": row["quoted_at"].isoformat(),
            "observed_at": row["observed_at"].isoformat(),
            "received_at": row["received_at"].isoformat(),
            "contract_version": row["contract_version"],
            "markets": row["markets_json"]["markets"],
            "sha256": row["sha256"],
        }
        return validate_market_snapshot_v1(document, self._schema)


class MarketEvaluationRepository:
    """Append post-prediction comparisons and result-revision settlements."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add_evaluation(self, evaluation: MarketEvaluation) -> UUID:
        unavailable = evaluation.eligibility in {
            EvaluationEligibility.MISSING,
            EvaluationEligibility.STALE,
            EvaluationEligibility.LATE,
        }
        if unavailable and evaluation.snapshot_id is not None:
            raise MarketContractError("unavailable Market states must not reference snapshots")
        if not unavailable and evaluation.snapshot_id is None:
            raise MarketContractError("eligible and unsupported evaluations require a snapshot")
        derived = {
            line.line_id: line.probabilities.to_dict() if line.probabilities is not None else None
            for line in evaluation.lines
        }
        eligibility = evaluation.eligibility.value

        evaluation_id = self._connection.execute(
            text(
                """
                INSERT INTO market.market_evaluations (
                    match_id, prediction_id, market_snapshot_id, evaluator_version,
                    derived_probabilities, eligibility, reason
                ) VALUES (
                    :match_id, :prediction_id, :market_snapshot_id, :evaluator_version,
                    CAST(:derived_probabilities AS jsonb), :eligibility, :reason
                )
                ON CONFLICT (prediction_id, market_snapshot_id, evaluator_version) DO NOTHING
                RETURNING id
                """
            ),
            {
                "match_id": UUID(evaluation.match_id),
                "prediction_id": UUID(evaluation.prediction_id),
                "market_snapshot_id": (
                    UUID(evaluation.snapshot_id) if evaluation.snapshot_id is not None else None
                ),
                "evaluator_version": evaluation.evaluator_version,
                "derived_probabilities": json.dumps(derived, sort_keys=True, separators=(",", ":")),
                "eligibility": eligibility,
                "reason": evaluation.reason,
            },
        ).scalar_one_or_none()
        if evaluation_id is not None:
            return cast(UUID, evaluation_id)
        existing = self._connection.execute(
            text(
                """
                SELECT id, derived_probabilities, eligibility, reason
                FROM market.market_evaluations
                WHERE prediction_id = :prediction_id
                  AND market_snapshot_id IS NOT DISTINCT FROM :market_snapshot_id
                  AND evaluator_version = :evaluator_version
                """
            ),
            {
                "prediction_id": UUID(evaluation.prediction_id),
                "market_snapshot_id": (
                    UUID(evaluation.snapshot_id) if evaluation.snapshot_id is not None else None
                ),
                "evaluator_version": evaluation.evaluator_version,
            },
        ).one()
        if (
            dict(existing[1]) != derived
            or existing[2] != eligibility
            or existing[3] != evaluation.reason
        ):
            raise MarketContractError("existing Market evaluation has different immutable content")
        return cast(UUID, existing[0])

    def add_settlement(self, settlement: MarketSettlement) -> UUID:
        settlement_id = self._connection.execute(
            text(
                """
                INSERT INTO market.market_settlements (
                    match_id, market_snapshot_id, line_id, result_revision_id,
                    evaluator_version, outcome, reason
                ) VALUES (
                    :match_id, :market_snapshot_id, :line_id, :result_revision_id,
                    :evaluator_version, :outcome, :reason
                )
                ON CONFLICT (
                    market_snapshot_id, line_id, result_revision_id, evaluator_version
                ) DO NOTHING
                RETURNING id
                """
            ),
            {
                "match_id": UUID(settlement.match_id),
                "market_snapshot_id": UUID(settlement.snapshot_id),
                "line_id": settlement.line_id,
                "result_revision_id": UUID(settlement.result_revision_id),
                "evaluator_version": settlement.evaluator_version,
                "outcome": settlement.outcome.value,
                "reason": settlement.reason,
            },
        ).scalar_one_or_none()
        if settlement_id is not None:
            return cast(UUID, settlement_id)
        existing = self._connection.execute(
            text(
                """
                SELECT id, match_id, outcome, reason
                FROM market.market_settlements
                WHERE market_snapshot_id = :market_snapshot_id
                  AND line_id = :line_id
                  AND result_revision_id = :result_revision_id
                  AND evaluator_version = :evaluator_version
                """
            ),
            {
                "market_snapshot_id": UUID(settlement.snapshot_id),
                "line_id": settlement.line_id,
                "result_revision_id": UUID(settlement.result_revision_id),
                "evaluator_version": settlement.evaluator_version,
            },
        ).one()
        if (
            existing[1] != UUID(settlement.match_id)
            or existing[2] != settlement.outcome.value
            or existing[3] != settlement.reason
        ):
            raise MarketContractError("existing Market settlement has different immutable content")
        return cast(UUID, existing[0])
