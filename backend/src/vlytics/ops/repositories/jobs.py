"""Atomic PostgreSQL job leasing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


class JobRepository:
    """Mutate only the queue state that is explicitly designed to change."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def enqueue(
        self,
        *,
        job_key: str,
        job_type: str,
        payload: Mapping[str, Any],
        due_at: datetime,
        deadline_at: datetime | None,
        match_id: UUID | None = None,
        schedule_revision_id: UUID | None = None,
        stage: str | None = None,
        variant_id: UUID | None = None,
    ) -> Mapping[str, Any]:
        """Create or return one uniquely keyed job without changing an existing row."""

        self._connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:job_key, 0))"),
            {"job_key": job_key},
        )
        parameters = {
            "job_key": job_key,
            "job_type": job_type,
            "payload": json.dumps(dict(payload), separators=(",", ":"), sort_keys=True),
            "due_at": due_at,
            "deadline_at": deadline_at,
            "match_id": match_id,
            "schedule_revision_id": schedule_revision_id,
            "stage": stage,
            "variant_id": variant_id,
        }
        row = (
            self._connection.execute(
                text(
                    """
                    INSERT INTO ops.jobs (
                        job_key, job_type, payload, due_at, deadline_at,
                        match_id, schedule_revision_id, stage, variant_id
                    ) VALUES (
                        :job_key, :job_type, CAST(:payload AS jsonb), :due_at, :deadline_at,
                        :match_id, :schedule_revision_id, :stage, :variant_id
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING *
                    """
                ),
                parameters,
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            row = (
                self._connection.execute(
                    text(
                        """
                        SELECT *
                        FROM ops.jobs
                        WHERE job_key = :job_key
                           OR (
                               CAST(:schedule_revision_id AS uuid) IS NOT NULL
                               AND CAST(:job_type AS text) IN (
                                   'engine.freeze_snapshot',
                                   'engine.run_prediction'
                               )
                               AND job_type = :job_type
                               AND schedule_revision_id = CAST(:schedule_revision_id AS uuid)
                               AND stage IS NOT DISTINCT FROM CAST(:stage AS text)
                               AND variant_id IS NOT DISTINCT FROM CAST(:variant_id AS uuid)
                           )
                        ORDER BY created_at
                        LIMIT 1
                        """
                    ),
                    parameters,
                )
                .mappings()
                .one()
            )
        return dict(row)

    def lease_next(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> Mapping[str, Any] | None:
        """Atomically lease one due job, recovering an expired lease if needed."""

        row = (
            self._connection.execute(
                text(
                    """
                WITH candidate AS (
                    SELECT id, state, lease_owner, lease_started_at, attempt_no
                    FROM ops.jobs
                    WHERE due_at <= :now
                      AND (deadline_at IS NULL OR deadline_at > :now)
                      AND (
                          state IN ('queued', 'retry_wait')
                          OR (state = 'running' AND lease_until <= :now)
                      )
                    ORDER BY due_at, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                ), abandoned AS (
                    INSERT INTO ops.job_attempts (
                        job_id, attempt_no, lease_owner, started_at, completed_at,
                        outcome, error_code, details
                    )
                    SELECT id, attempt_no, lease_owner, lease_started_at, :now,
                           'abandoned', 'lease_expired', CAST(:recovery_details AS jsonb)
                    FROM candidate
                    WHERE state = 'running'
                    ON CONFLICT (job_id, attempt_no) DO NOTHING
                )
                UPDATE ops.jobs AS job
                SET state = 'running',
                    lease_owner = :lease_owner,
                    lease_started_at = :now,
                    lease_until = :lease_until,
                    attempt_no = job.attempt_no + 1,
                    error_code = NULL,
                    updated_at = :now
                FROM candidate
                WHERE job.id = candidate.id
                RETURNING job.*
                """
                ),
                {
                    "now": now,
                    "lease_owner": lease_owner,
                    "lease_until": now + lease_duration,
                    "recovery_details": json.dumps({"recovered": True}),
                },
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else dict(row)

    def finish(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        succeeded: bool,
        error_code: str | None,
        completed_at: datetime,
    ) -> bool:
        """Finish a job only when the caller still owns its active lease."""

        attempt = self._active_attempt(job_id=job_id, lease_owner=lease_owner)
        result = self._connection.execute(
            text(
                """
                UPDATE ops.jobs
                SET state = CASE
                        WHEN deadline_at IS NOT NULL AND deadline_at <= :completed_at
                        THEN 'expired' ELSE :state END,
                    lease_owner = NULL,
                    lease_until = NULL,
                    lease_started_at = NULL,
                    error_code = CASE
                        WHEN deadline_at IS NOT NULL AND deadline_at <= :completed_at
                        THEN 'deadline_expired' ELSE :error_code END,
                    updated_at = :completed_at
                WHERE id = :job_id
                  AND state = 'running'
                  AND lease_owner = :lease_owner
                  AND lease_until > :completed_at
                """
            ),
            {
                "job_id": job_id,
                "lease_owner": lease_owner,
                "state": "succeeded" if succeeded else "failed",
                "error_code": error_code,
                "completed_at": completed_at,
            },
        )
        finished = result.rowcount == 1
        if finished and attempt is not None:
            self._record_attempt(
                job_id=job_id,
                attempt_no=attempt[0],
                lease_owner=lease_owner,
                started_at=attempt[1],
                outcome="succeeded" if succeeded else "failed",
                error_code=error_code,
                completed_at=completed_at,
            )
        return finished

    def schedule_retry(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        retry_at: datetime,
        error_code: str,
        now: datetime,
    ) -> bool:
        """Release an owned lease into a durable retry wait."""

        attempt = self._active_attempt(job_id=job_id, lease_owner=lease_owner)
        result = self._connection.execute(
            text(
                """
                UPDATE ops.jobs
                SET state = CASE
                        WHEN deadline_at IS NOT NULL AND deadline_at <= :retry_at
                        THEN 'expired' ELSE 'retry_wait' END,
                    due_at = CASE
                        WHEN deadline_at IS NOT NULL AND deadline_at <= :retry_at
                        THEN due_at ELSE :retry_at END,
                    lease_owner = NULL,
                    lease_until = NULL,
                    lease_started_at = NULL,
                    error_code = CASE
                        WHEN deadline_at IS NOT NULL AND deadline_at <= :retry_at
                        THEN 'deadline_expired' ELSE :error_code END,
                    updated_at = :now
                WHERE id = :job_id
                  AND state = 'running'
                  AND lease_owner = :lease_owner
                  AND lease_until > :now
                """
            ),
            {
                "job_id": job_id,
                "lease_owner": lease_owner,
                "retry_at": retry_at,
                "error_code": error_code,
                "now": now,
            },
        )
        scheduled = result.rowcount == 1
        if scheduled and attempt is not None:
            self._record_attempt(
                job_id=job_id,
                attempt_no=attempt[0],
                lease_owner=lease_owner,
                started_at=attempt[1],
                outcome="timed_out" if error_code == "timeout" else "failed",
                error_code=error_code,
                completed_at=now,
            )
        return scheduled

    def expire_due(self, *, now: datetime) -> int:
        """Expire queued work and abandoned leases after their deadline."""

        result = self._connection.execute(
            text(
                """
                WITH candidates AS (
                    SELECT id, state, attempt_no, lease_owner, lease_started_at
                    FROM ops.jobs
                    WHERE deadline_at <= :now
                      AND (
                          state IN ('queued', 'retry_wait')
                          OR (state = 'running' AND lease_until <= :now)
                      )
                    FOR UPDATE SKIP LOCKED
                ), abandoned AS (
                    INSERT INTO ops.job_attempts (
                        job_id, attempt_no, lease_owner, started_at, completed_at,
                        outcome, error_code, details
                    )
                    SELECT id, attempt_no, lease_owner, lease_started_at, :now,
                           'abandoned', 'deadline_expired', CAST(:expiration_details AS jsonb)
                    FROM candidates
                    WHERE state = 'running'
                    ON CONFLICT (job_id, attempt_no) DO NOTHING
                )
                UPDATE ops.jobs AS job
                SET state = 'expired',
                    lease_owner = NULL,
                    lease_until = NULL,
                    lease_started_at = NULL,
                    error_code = 'deadline_expired',
                    updated_at = :now
                FROM candidates
                WHERE job.id = candidates.id
                """
            ),
            {
                "now": now,
                "expiration_details": json.dumps({"expired": True}),
            },
        )
        return result.rowcount

    def quarantine(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        error_code: str,
        now: datetime,
    ) -> bool:
        """Stop work that requires operator review."""

        attempt = self._active_attempt(job_id=job_id, lease_owner=lease_owner)
        result = self._connection.execute(
            text(
                """
                UPDATE ops.jobs
                SET state = 'quarantined',
                    lease_owner = NULL,
                    lease_until = NULL,
                    lease_started_at = NULL,
                    error_code = :error_code,
                    updated_at = :now
                WHERE id = :job_id
                  AND (
                      state IN ('queued', 'retry_wait')
                      OR (
                          state = 'running'
                          AND lease_owner = :lease_owner
                          AND lease_until > :now
                      )
                  )
                """
            ),
            {
                "job_id": job_id,
                "lease_owner": lease_owner,
                "error_code": error_code,
                "now": now,
            },
        )
        quarantined = result.rowcount == 1
        if quarantined and attempt is not None:
            self._record_attempt(
                job_id=job_id,
                attempt_no=attempt[0],
                lease_owner=lease_owner,
                started_at=attempt[1],
                outcome="failed",
                error_code=error_code,
                completed_at=now,
            )
        return quarantined

    def cancel_schedule_revision(
        self,
        *,
        schedule_revision_id: UUID,
        now: datetime,
        error_code: str,
    ) -> int:
        """Cancel every unfinished job owned by an obsolete schedule revision."""

        result = self._connection.execute(
            text(
                """
                UPDATE ops.jobs
                SET state = 'cancelled',
                    lease_owner = NULL,
                    lease_until = NULL,
                    lease_started_at = NULL,
                    error_code = :error_code,
                    updated_at = :now
                WHERE schedule_revision_id = CAST(:schedule_revision_id AS uuid)
                  AND state IN ('queued', 'running', 'retry_wait')
                """
            ),
            {
                "schedule_revision_id": schedule_revision_id,
                "error_code": error_code,
                "now": now,
            },
        )
        return result.rowcount

    def cancel_match_jobs(
        self,
        *,
        match_id: UUID,
        now: datetime,
        error_code: str,
        except_schedule_revision_id: UUID | None = None,
    ) -> int:
        """Cancel unfinished work for every affected schedule revision of a match."""

        result = self._connection.execute(
            text(
                """
                UPDATE ops.jobs
                SET state = 'cancelled',
                    lease_owner = NULL,
                    lease_until = NULL,
                    lease_started_at = NULL,
                    error_code = :error_code,
                    updated_at = :now
                WHERE match_id = :match_id
                  AND state IN ('queued', 'running', 'retry_wait')
                  AND (
                      CAST(:except_schedule_revision_id AS uuid) IS NULL
                      OR schedule_revision_id <> CAST(:except_schedule_revision_id AS uuid)
                  )
                """
            ),
            {
                "match_id": match_id,
                "except_schedule_revision_id": except_schedule_revision_id,
                "error_code": error_code,
                "now": now,
            },
        )
        return result.rowcount

    def owns_active_lease(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
        now: datetime,
    ) -> bool:
        """Return whether a worker still owns a live running lease."""

        return (
            self._connection.execute(
                text(
                    """
                    SELECT 1
                    FROM ops.jobs
                    WHERE id = :job_id
                      AND state = 'running'
                      AND lease_owner = :lease_owner
                      AND lease_until > :now
                    FOR UPDATE
                    """
                ),
                {"job_id": job_id, "lease_owner": lease_owner, "now": now},
            ).scalar_one_or_none()
            is not None
        )

    def _record_attempt(
        self,
        *,
        job_id: UUID,
        attempt_no: int,
        lease_owner: str,
        started_at: datetime,
        outcome: str,
        error_code: str | None,
        completed_at: datetime,
    ) -> None:
        self._connection.execute(
            text(
                """
                INSERT INTO ops.job_attempts (
                    job_id, attempt_no, lease_owner, started_at, completed_at,
                    outcome, error_code
                ) VALUES (
                    :job_id, :attempt_no, :lease_owner, :started_at, :completed_at,
                    :outcome, :error_code
                )
                ON CONFLICT (job_id, attempt_no) DO NOTHING
                """
            ),
            {
                "job_id": job_id,
                "attempt_no": attempt_no,
                "lease_owner": lease_owner,
                "started_at": started_at,
                "outcome": outcome,
                "error_code": error_code,
                "completed_at": completed_at,
            },
        )

    def _active_attempt(
        self,
        *,
        job_id: UUID,
        lease_owner: str,
    ) -> tuple[int, datetime] | None:
        row = self._connection.execute(
            text(
                """
                SELECT attempt_no, lease_started_at
                FROM ops.jobs
                WHERE id = :job_id
                  AND state = 'running'
                  AND lease_owner = :lease_owner
                """
            ),
            {"job_id": job_id, "lease_owner": lease_owner},
        ).one_or_none()
        if row is None:
            return None
        attempt_no, started_at = row
        if not isinstance(started_at, datetime):
            raise TypeError("active job lease has no start timestamp")
        return int(attempt_no), started_at
