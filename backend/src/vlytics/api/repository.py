"""Read-side repository contracts and a PostgreSQL implementation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import Engine, text


@dataclass(frozen=True)
class ReadSnapshot:
    """One internally consistent read view used for filtering and aggregation."""

    matches: tuple[Mapping[str, Any], ...] = ()
    predictions: tuple[Mapping[str, Any], ...] = ()
    evaluations: tuple[Mapping[str, Any], ...] = ()
    operations: tuple[Mapping[str, Any], ...] = ()
    coverage: tuple[Mapping[str, Any], ...] = ()
    revision: str = "empty"
    budgets: tuple[Mapping[str, Any], ...] = ()


class ReadRepository(Protocol):
    def load(self) -> ReadSnapshot:
        """Load a transactionally consistent view."""

    def request_retry(
        self, *, job_id: str, idempotency_key: str, requested_at: datetime
    ) -> Mapping[str, Any]:
        """Atomically request one deadline-safe retry."""


class RetryJobNotFoundError(LookupError):
    """The requested job does not exist."""


class RetryJobConflictError(RuntimeError):
    """The idempotency key was already used for another job."""


class RetryJobNotAllowedError(RuntimeError):
    """The job state or deadline does not allow a retry."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class InMemoryReadRepository:
    """Synthetic-data repository for contract tests and local UI development."""

    snapshot: ReadSnapshot = field(default_factory=ReadSnapshot)
    retry_requests: dict[str, str] = field(default_factory=dict)

    def load(self) -> ReadSnapshot:
        return self.snapshot

    def request_retry(
        self, *, job_id: str, idempotency_key: str, requested_at: datetime
    ) -> Mapping[str, Any]:
        previous_job_id = self.retry_requests.get(idempotency_key)
        if previous_job_id is not None:
            if previous_job_id != job_id:
                raise RetryJobConflictError
            current = next(
                (row for row in self.snapshot.operations if str(row.get("id")) == job_id), None
            )
            if current is None:
                raise RetryJobNotFoundError
            return {**current, "job_id": job_id, "idempotent_replay": True}

        current = next(
            (row for row in self.snapshot.operations if str(row.get("id")) == job_id), None
        )
        if current is None:
            raise RetryJobNotFoundError
        deadline = current.get("deadline_at")
        if isinstance(deadline, datetime) and deadline <= requested_at:
            raise RetryJobNotAllowedError("retry_deadline_expired")
        if current.get("state") != "failed":
            raise RetryJobNotAllowedError("job_not_retryable")

        updated = {
            **current,
            "state": "retry_wait",
            "due_at": requested_at,
            "error_code": None,
            "updated_at": requested_at,
        }
        self.snapshot = replace(
            self.snapshot,
            operations=tuple(
                updated if str(row.get("id")) == job_id else row for row in self.snapshot.operations
            ),
            revision=f"{self.snapshot.revision}:retry:{job_id}:{idempotency_key}",
        )
        self.retry_requests[idempotency_key] = job_id
        return {**updated, "job_id": job_id, "idempotent_replay": False}


class PostgresReadRepository:
    """Read normalized facts through the restricted read-API database role."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self) -> ReadSnapshot:
        # REPEATABLE READ prevents cursor pages from mixing rows inside this response. The cursor
        # also carries a revision fingerprint and is rejected if the next request sees new data.
        with self._engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            transaction = connection.begin()
            try:
                matches = tuple(
                    dict(row) for row in connection.execute(text(_MATCH_SQL)).mappings()
                )
                predictions = tuple(
                    dict(row) for row in connection.execute(text(_PREDICTION_SQL)).mappings()
                )
                evaluations = tuple(
                    dict(row) for row in connection.execute(text(_EVALUATION_SQL)).mappings()
                )
                operations = tuple(
                    dict(row) for row in connection.execute(text(_OPERATION_SQL)).mappings()
                )
                coverage = tuple(
                    dict(row) for row in connection.execute(text(_COVERAGE_SQL)).mappings()
                )
                budgets = tuple(
                    dict(row) for row in connection.execute(text(_BUDGET_SQL)).mappings()
                )
                revision = str(connection.execute(text(_REVISION_SQL)).scalar_one())
                transaction.commit()
            except BaseException:
                transaction.rollback()
                raise
        return ReadSnapshot(
            matches, predictions, evaluations, operations, coverage, revision, budgets
        )

    def request_retry(
        self, *, job_id: str, idempotency_key: str, requested_at: datetime
    ) -> Mapping[str, Any]:
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT outcome, job_id::text, state, due_at, deadline_at,
                               idempotent_replay
                        FROM ops.request_job_retry(
                            CAST(:job_id AS uuid), :idempotency_key, :requested_at
                        )
                        """
                    ),
                    {
                        "job_id": job_id,
                        "idempotency_key": idempotency_key,
                        "requested_at": requested_at,
                    },
                )
                .mappings()
                .one()
            )
        outcome = str(row["outcome"])
        if outcome == "job_not_found":
            raise RetryJobNotFoundError
        if outcome == "idempotency_conflict":
            raise RetryJobConflictError
        if outcome in {"retry_deadline_expired", "job_not_retryable"}:
            raise RetryJobNotAllowedError(outcome)
        if outcome != "scheduled":
            raise RuntimeError("unexpected retry outcome")
        return dict(row)


_MATCH_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (match_id) * FROM mirror.match_revisions
    ORDER BY match_id, revision DESC
), latest_result AS (
    SELECT DISTINCT ON (match_id) * FROM mirror.result_revisions
    ORDER BY match_id, revision DESC
), latest_market AS (
    SELECT DISTINCT ON (match_id) * FROM market.market_snapshots
    ORDER BY match_id, received_at DESC, id DESC
)
SELECT m.id::text, m.source_match_code, c.source_competition_code AS competition,
       c.division, c.stage, latest.id::text AS schedule_revision_id,
       latest.scheduled_start_at, latest.actual_start_at, latest.status,
       latest.raw_snapshot_id::text AS source_snapshot_id,
       home.id::text AS home_team_id, home.source_team_code AS home_team_code,
       coalesce(home.display_name, home.source_team_code) AS home_team_name,
       away.id::text AS away_team_id, away.source_team_code AS away_team_code,
       coalesce(away.display_name, away.source_team_code) AS away_team_name,
       venue.name AS venue, latest_result.id::text AS result_revision_id,
       CASE WHEN latest_result.id IS NULL THEN NULL ELSE jsonb_build_object(
           'home_sets', latest_result.home_sets, 'away_sets', latest_result.away_sets,
           'home_points', latest_result.home_points, 'away_points', latest_result.away_points,
           'finality', latest_result.finality) END AS result,
       latest_market.id::text AS market_snapshot_id, latest_market.source AS market_source,
       latest_market.quoted_at AS market_quoted_at
FROM mirror.matches m
JOIN mirror.competitions c ON c.id = m.competition_id
JOIN latest ON latest.match_id = m.id
JOIN mirror.team_identities home ON home.id = latest.home_team_id
JOIN mirror.team_identities away ON away.id = latest.away_team_id
LEFT JOIN mirror.venues venue ON venue.id = latest.venue_id
LEFT JOIN latest_result ON latest_result.match_id = m.id
LEFT JOIN latest_market ON latest_market.match_id = m.id
"""

_PREDICTION_SQL = """
SELECT p.id::text, p.match_id::text, c.source_competition_code AS competition,
       c.division, v.provider, p.stage AS prediction_type, v.requested_model,
       p.resolved_model_id, v.pinned_model_version AS model_version,
       v.prompt_version, v.feature_version, p.schedule_revision_id::text,
       s.id::text AS feature_snapshot_id, mr.raw_snapshot_id::text AS source_snapshot_id,
       p.generated_at, coalesce(ps.current_status, 'published') AS status,
       'succeeded'::text AS provider_status, NULL::text AS error_code,
       p.output_json AS output
FROM engine.predictions p
JOIN engine.model_variants v ON v.id = p.variant_id
JOIN mirror.matches m ON m.id = p.match_id
JOIN mirror.competitions c ON c.id = m.competition_id
JOIN engine.feature_snapshots s ON s.id = p.snapshot_id
JOIN mirror.match_revisions mr ON mr.id = p.schedule_revision_id
LEFT JOIN engine.prediction_status_projection ps ON ps.prediction_id = p.id
UNION ALL
SELECT a.id::text, s.match_id::text, c.source_competition_code AS competition,
       c.division, v.provider, coalesce(j.stage, 'provider_attempt') AS prediction_type,
       v.requested_model, v.requested_model AS resolved_model_id,
       v.pinned_model_version AS model_version, v.prompt_version, v.feature_version,
       s.schedule_revision_id::text, s.id::text AS feature_snapshot_id,
       mr.raw_snapshot_id::text AS source_snapshot_id,
       coalesce(a.completed_at, a.started_at) AS generated_at,
       a.status, a.status AS provider_status, a.error_code, '{}'::jsonb AS output
FROM engine.prediction_attempts a
JOIN engine.feature_snapshots s ON s.id = a.snapshot_id
JOIN engine.model_variants v ON v.id = a.variant_id
JOIN mirror.matches m ON m.id = s.match_id
JOIN mirror.competitions c ON c.id = m.competition_id
JOIN mirror.match_revisions mr ON mr.id = s.schedule_revision_id
JOIN ops.jobs j ON j.id = a.job_id
WHERE a.status <> 'succeeded'
"""

_EVALUATION_SQL = """
SELECT e.id::text, e.prediction_id::text, e.result_revision_id::text,
       rr.revision AS result_revision_number, rr.finality AS result_finality,
       e.evaluator_version, e.cohort_policy_version, e.metric_values, e.settlement,
       e.created_at AS evaluation_created_at,
       p.match_id::text, p.stage AS prediction_type, v.provider,
       coalesce(v.pinned_model_version, p.resolved_model_id) AS model_version,
       v.prompt_version, s.feature_version, s.availability_policy,
       c.division, c.source_competition_code AS competition, c.stage,
       p.schedule_revision_id::text, p.snapshot_id::text AS feature_snapshot_id,
       p.input_cutoff_at, mr.scheduled_start_at AS match_start_at,
       mr.raw_snapshot_id::text AS source_snapshot_id,
       CASE WHEN me.eligibility IS NULL THEN 'missing' ELSE me.eligibility END
           AS market_availability,
       market_snapshot.markets_json AS market_json
FROM engine.evaluations e
JOIN engine.predictions p ON p.id = e.prediction_id
JOIN engine.model_variants v ON v.id = p.variant_id
JOIN engine.feature_snapshots s ON s.id = p.snapshot_id
JOIN mirror.matches m ON m.id = p.match_id
JOIN mirror.competitions c ON c.id = m.competition_id
JOIN mirror.match_revisions mr ON mr.id = p.schedule_revision_id
JOIN mirror.result_revisions rr ON rr.id = e.result_revision_id
LEFT JOIN LATERAL (
    SELECT eligibility FROM market.market_evaluations
    WHERE prediction_id = p.id ORDER BY created_at DESC LIMIT 1
) me ON true
LEFT JOIN market.market_snapshots market_snapshot ON market_snapshot.id = p.market_snapshot_id
"""

_OPERATION_SQL = """
SELECT id::text, job_type, state, due_at, deadline_at, attempt_no, error_code,
       schedule_revision_id::text, match_id::text, updated_at
FROM ops.jobs
"""

_COVERAGE_SQL = """
SELECT id::text, data_kind, availability, observed_at,
       split_part(evidence, ' ', 1) AS evidence_code,
       raw_snapshot_id::text AS source_snapshot_id, match_id::text
FROM mirror.source_coverage
"""

_BUDGET_SQL = """
WITH base AS (
    SELECT provider, currency, budget_day, budget_month, reserved_at, settled_at,
           reserved_amount, settled_amount, state, conservative_charge
    FROM ops.provider_budget_reservations
)
SELECT provider, currency, 'day'::text AS period,
       budget_day AS period_start, budget_day + 1 AS period_end,
       max(coalesce(settled_at, reserved_at)) AS as_of,
       sum(reserved_amount) AS reserved_amount,
       sum(coalesce(settled_amount, 0)) AS actual_amount,
       sum(coalesce(settled_amount, reserved_amount)) AS effective_amount,
       count(*)::integer AS reservation_count,
       count(*) FILTER (WHERE state = 'settled')::integer AS settled_count,
       count(*) FILTER (WHERE state = 'reserved')::integer AS outstanding_count,
       count(*) FILTER (WHERE conservative_charge)::integer AS conservative_charge_count
FROM base
GROUP BY provider, currency, budget_day
UNION ALL
SELECT provider, currency, 'month'::text AS period,
       budget_month AS period_start,
       (budget_month + interval '1 month')::date AS period_end,
       max(coalesce(settled_at, reserved_at)) AS as_of,
       sum(reserved_amount) AS reserved_amount,
       sum(coalesce(settled_amount, 0)) AS actual_amount,
       sum(coalesce(settled_amount, reserved_amount)) AS effective_amount,
       count(*)::integer AS reservation_count,
       count(*) FILTER (WHERE state = 'settled')::integer AS settled_count,
       count(*) FILTER (WHERE state = 'reserved')::integer AS outstanding_count,
       count(*) FILTER (WHERE conservative_charge)::integer AS conservative_charge_count
FROM base
GROUP BY provider, currency, budget_month
ORDER BY provider, period, period_start DESC
"""

_REVISION_SQL = """
SELECT md5(concat_ws('|',
    coalesce((SELECT count(*)::text || ':' || max(created_at)::text
              FROM mirror.match_revisions), '0:'),
    coalesce((SELECT count(*)::text || ':' || max(created_at)::text
              FROM mirror.result_revisions), '0:'),
    coalesce((SELECT count(*)::text || ':' || max(created_at)::text
              FROM engine.predictions), '0:'),
    coalesce((SELECT count(*)::text || ':' || max(created_at)::text
              FROM engine.prediction_attempts), '0:'),
    coalesce((SELECT count(*)::text || ':' || max(created_at)::text
              FROM engine.evaluations), '0:'),
    coalesce((SELECT count(*)::text || ':' || max(created_at)::text
              FROM market.market_snapshots), '0:'),
    coalesce((SELECT count(*)::text || ':' || max(created_at)::text
              FROM market.market_evaluations), '0:'),
    coalesce((SELECT count(*)::text || ':' || max(coalesce(settled_at, created_at))::text
              FROM ops.provider_budget_reservations), '0:'),
    coalesce((SELECT count(*)::text || ':' || max(updated_at)::text
              FROM ops.jobs), '0:'),
    coalesce((SELECT count(*)::text || ':' || max(created_at)::text
              FROM mirror.source_coverage), '0:')
))
"""


def object_mapping(value: object) -> dict[str, object]:
    """Normalize JSONB driver values without accepting unexpected scalar payloads."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): cast(object, item) for key, item in value.items()}


def aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be a timezone-aware datetime")
    return value
