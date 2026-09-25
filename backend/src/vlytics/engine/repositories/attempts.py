"""Append-only persistence mapping for provider prediction attempts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Connection, text

from vlytics.engine.providers.models import (
    AttemptStatus,
    FailureCode,
    PredictionAttemptResult,
)


@dataclass(frozen=True)
class PredictionAttemptRecord:
    job_id: UUID
    snapshot_id: UUID
    variant_id: UUID
    started_at: datetime
    completed_at: datetime
    status: str
    error_code: str | None
    request_hash: str
    response_hash: str | None
    usage_json: dict[str, Any]
    latency_ms: int

    @classmethod
    def from_result(
        cls,
        result: PredictionAttemptResult,
        *,
        job_id: UUID,
        snapshot_id: UUID,
        variant_id: UUID,
        started_at: datetime,
        completed_at: datetime,
    ) -> PredictionAttemptRecord:
        if completed_at < started_at:
            raise ValueError("prediction attempt completion precedes its start")
        usage = result.usage
        usage_json: dict[str, Any] = {
            "provider": result.provider.value,
            "registry_variant_id": result.variant_id,
            "requested_model_id": result.requested_model_id,
            "pinned_model_version": result.pinned_model_version,
            "resolved_model_id": result.resolved_model_id,
            "version_policy": result.version_policy.value,
            "prompt_version": result.prompt_version,
            "prompt_hash": result.prompt_hash,
            "provider_request_id": result.request_id,
            "input_tokens": usage.input_tokens if usage is not None else None,
            "output_tokens": usage.output_tokens if usage is not None else None,
            "cost_amount": str(result.cost),
        }
        return cls(
            job_id=job_id,
            snapshot_id=snapshot_id,
            variant_id=variant_id,
            started_at=started_at,
            completed_at=completed_at,
            status=_database_status(result),
            error_code=result.failure_code.value if result.failure_code is not None else None,
            request_hash=result.request_hash,
            response_hash=result.response_hash,
            usage_json=usage_json,
            latency_ms=result.latency_ms,
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "snapshot_id": self.snapshot_id,
            "variant_id": self.variant_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "error_code": self.error_code,
            "request_hash": self.request_hash,
            "response_hash": self.response_hash,
            "usage_json": json.dumps(
                self.usage_json,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "latency_ms": self.latency_ms,
        }


class PredictionAttemptRepository:
    """Insert immutable provider attempts, including all preflight skips."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(self, record: PredictionAttemptRecord) -> UUID:
        attempt_id = self._connection.execute(
            text(
                """
                INSERT INTO engine.prediction_attempts (
                    job_id, snapshot_id, variant_id, started_at, completed_at,
                    status, error_code, request_hash, response_hash, usage_json, latency_ms
                ) VALUES (
                    :job_id, :snapshot_id, :variant_id, :started_at, :completed_at,
                    :status, :error_code, :request_hash, :response_hash,
                    CAST(:usage_json AS jsonb), :latency_ms
                )
                RETURNING id
                """
            ),
            record.parameters(),
        ).scalar_one()
        return cast(UUID, attempt_id)


def _database_status(result: PredictionAttemptResult) -> str:
    if result.status is AttemptStatus.SUCCEEDED:
        return "succeeded"
    if result.failure_code is FailureCode.TIMEOUT:
        return "timed_out"
    if result.failure_code is FailureCode.BUDGET_SKIPPED:
        return "budget_skipped"
    if result.status is AttemptStatus.SKIPPED:
        return "skipped"
    return "failed"
