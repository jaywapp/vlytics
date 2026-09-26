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
    scoped_page: bool = False
    has_more: bool = False


@dataclass(frozen=True)
class ReadQuery:
    """Database-side boundary for one API endpoint."""

    endpoint: str
    filters: Mapping[str, object] = field(default_factory=dict)
    boundary: tuple[datetime, str] | None = None
    limit: int | None = None


class ReadRepository(Protocol):
    def load(self) -> ReadSnapshot:
        """Load a transactionally consistent view."""

    def load_query(self, query: ReadQuery) -> ReadSnapshot:
        """Load an endpoint-scoped view when the repository supports SQL filtering."""

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

    def load_query(self, query: ReadQuery) -> ReadSnapshot:
        del query
        return self.load()

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

    def load_query(self, query: ReadQuery) -> ReadSnapshot:
        with self._engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            transaction = connection.begin()
            try:
                snapshot = self._load_query(connection, query)
                transaction.commit()
            except BaseException:
                transaction.rollback()
                raise
        return snapshot

    def _load_query(self, connection: Any, query: ReadQuery) -> ReadSnapshot:
        filters = dict(query.filters)
        revision_sql = _ENDPOINT_REVISION_SQL.get(query.endpoint, _REVISION_SQL)
        revision = str(connection.execute(text(revision_sql)).scalar_one())
        empty: tuple[Mapping[str, Any], ...] = ()

        if query.endpoint in {"schedule", "match"}:
            clauses: list[str] = []
            params: dict[str, object] = {}
            if query.endpoint == "match":
                clauses.append("scoped.id = :match_id")
                params["match_id"] = filters["match_id"]
            else:
                clauses.extend(
                    [
                        "scoped.scheduled_start_at >= :start_at",
                        "scoped.scheduled_start_at < :end_at",
                    ]
                )
                params.update(start_at=filters["start_at"], end_at=filters["end_at"])
                _append_filter(clauses, params, "scoped.division", "division", filters)
                _append_filter(clauses, params, "scoped.competition", "competition", filters)
                team = filters.get("team")
                if team is not None:
                    clauses.append(
                        ":team IN (scoped.home_team_id, scoped.home_team_code, "
                        "scoped.away_team_id, scoped.away_team_code)"
                    )
                    params["team"] = team
            matches = _rows(connection, _scoped_sql(_MATCH_SQL, clauses), params)
            match_ids = [str(row["id"]) for row in matches]
            predictions = (
                _rows(
                    connection,
                    _scoped_sql(
                        _PREDICTION_SQL,
                        ["scoped.match_id = ANY(CAST(:match_ids AS text[]))"],
                    ),
                    {"match_ids": match_ids},
                )
                if match_ids
                else empty
            )
            coverage = empty
            if query.endpoint == "match" and match_ids:
                coverage = _rows(
                    connection,
                    _scoped_sql(_COVERAGE_SQL, ["scoped.match_id = :match_id"]),
                    {"match_id": match_ids[0]},
                )
            return ReadSnapshot(matches, predictions, empty, empty, coverage, revision)

        if query.endpoint == "predictions":
            clauses = []
            params = {}
            _append_filter(clauses, params, "scoped.division", "division", filters)
            _append_filter(clauses, params, "scoped.competition", "competition", filters)
            _append_filter(clauses, params, "scoped.provider", "provider", filters)
            _append_filter(clauses, params, "scoped.prediction_type", "prediction_type", filters)
            _append_filter(clauses, params, "scoped.prompt_version", "prompt_version", filters)
            if filters.get("model") is not None:
                clauses.append(
                    ":model IN (scoped.requested_model, scoped.resolved_model_id, "
                    "scoped.model_version)"
                )
                params["model"] = filters["model"]
            if filters.get("start_at") is not None:
                clauses.append("scoped.generated_at >= :start_at")
                params["start_at"] = filters["start_at"]
            if filters.get("end_at") is not None:
                clauses.append("scoped.generated_at < :end_at")
                params["end_at"] = filters["end_at"]
            if filters.get("team") is not None:
                clauses.append(
                    ":team IN (scoped.home_team_id, scoped.home_team_code, "
                    "scoped.away_team_id, scoped.away_team_code)"
                )
                params["team"] = filters["team"]
            if query.boundary is not None:
                clauses.append(
                    "(scoped.generated_at, scoped.id::uuid) < "
                    "(:boundary_at, CAST(:boundary_id AS uuid))"
                )
                params.update(boundary_at=query.boundary[0], boundary_id=query.boundary[1])
            params["row_limit"] = (query.limit or 25) + 1
            predictions = _rows(
                connection,
                _scoped_sql(
                    _PREDICTION_SQL,
                    clauses,
                    "ORDER BY scoped.generated_at DESC, scoped.id::uuid DESC LIMIT :row_limit",
                ),
                params,
            )
            has_more = len(predictions) > (query.limit or 25)
            predictions = predictions[: query.limit]
            prediction_ids = [
                str(row["prediction_revision_id"])
                for row in predictions
                if row.get("prediction_revision_id")
            ]
            match_ids = sorted({str(row["match_id"]) for row in predictions})
            matches = (
                _rows(
                    connection,
                    _scoped_sql(_MATCH_SQL, ["scoped.id = ANY(CAST(:ids AS text[]))"]),
                    {"ids": match_ids},
                )
                if match_ids
                else empty
            )
            evaluations = (
                _rows(
                    connection,
                    _scoped_sql(
                        _EVALUATION_SQL,
                        ["scoped.prediction_id = ANY(CAST(:ids AS text[]))"],
                    ),
                    {"ids": prediction_ids},
                )
                if prediction_ids
                else empty
            )
            return ReadSnapshot(
                matches,
                predictions,
                evaluations,
                empty,
                empty,
                revision,
                scoped_page=True,
                has_more=has_more,
            )

        if query.endpoint == "performance":
            clauses = []
            params = {}
            for name, column in (
                ("division", "scoped.division"),
                ("competition", "scoped.competition"),
                ("stage", "scoped.stage"),
                ("feature_version", "scoped.feature_version"),
                ("availability_policy", "scoped.availability_policy"),
                ("evaluator_version", "scoped.evaluator_version"),
                ("result_finality", "scoped.result_finality"),
            ):
                _append_filter(clauses, params, column, name, filters)
            if filters.get("timing_eligibility") is not None:
                clauses.append(
                    "scoped.metric_values->'cohort'->>'timing_eligibility' = :timing_eligibility"
                )
                params["timing_eligibility"] = filters["timing_eligibility"]
            if filters.get("start_at") is not None:
                clauses.append("scoped.match_start_at >= :start_at")
                params["start_at"] = filters["start_at"]
            if filters.get("end_at") is not None:
                clauses.append("scoped.match_start_at < :end_at")
                params["end_at"] = filters["end_at"]
            evaluations = _rows(connection, _scoped_sql(_EVALUATION_SQL, clauses), params)
            evaluation_match_ids = sorted({str(row["match_id"]) for row in evaluations})
            predictions = empty
            if evaluation_match_ids:
                prediction_clauses = [
                    "scoped.match_id = ANY(CAST(:evaluation_match_ids AS text[]))"
                ]
                prediction_params: dict[str, object] = {
                    "evaluation_match_ids": evaluation_match_ids
                }
                for name, column in (
                    ("division", "scoped.division"),
                    ("competition", "scoped.competition"),
                    ("stage", "scoped.competition_stage"),
                    ("feature_version", "scoped.feature_version"),
                    ("availability_policy", "scoped.availability_policy"),
                ):
                    _append_filter(prediction_clauses, prediction_params, column, name, filters)
                selected_conditions: list[str] = []
                if filters.get("provider") is not None:
                    selected_conditions.append("scoped.provider = :selected_provider")
                    prediction_params["selected_provider"] = filters["provider"]
                if filters.get("model") is not None:
                    selected_conditions.append(
                        ":selected_model IN (scoped.requested_model, "
                        "scoped.resolved_model_id, scoped.model_version)"
                    )
                    prediction_params["selected_model"] = filters["model"]
                if filters.get("prompt_version") is not None:
                    selected_conditions.append("scoped.prompt_version = :selected_prompt_version")
                    prediction_params["selected_prompt_version"] = filters["prompt_version"]
                if filters.get("prediction_type") is not None:
                    selected_conditions.append("scoped.prediction_type = :selected_prediction_type")
                    prediction_params["selected_prediction_type"] = filters["prediction_type"]
                if selected_conditions:
                    selected_clause = "(" + " AND ".join(selected_conditions) + ")"
                    if filters.get("provider") == "statistical":
                        prediction_clauses.append(selected_clause)
                    else:
                        prediction_clauses.append(
                            "(scoped.provider = 'statistical' OR " + selected_clause + ")"
                        )
                predictions = _rows(
                    connection,
                    _scoped_sql(_PREDICTION_SQL, prediction_clauses),
                    prediction_params,
                )
            return ReadSnapshot(empty, predictions, evaluations, empty, empty, revision)

        if query.endpoint == "operations":
            clauses = []
            params = {}
            _append_filter(clauses, params, "scoped.state", "state", filters)
            _append_filter(clauses, params, "scoped.job_type", "job_type", filters)
            if query.boundary is not None:
                clauses.append(
                    "(scoped.due_at, scoped.id::uuid) > (:boundary_at, CAST(:boundary_id AS uuid))"
                )
                params.update(boundary_at=query.boundary[0], boundary_id=query.boundary[1])
            params["row_limit"] = (query.limit or 25) + 1
            operations = _rows(
                connection,
                _scoped_sql(
                    _OPERATION_SQL,
                    clauses,
                    "ORDER BY scoped.due_at, scoped.id::uuid LIMIT :row_limit",
                ),
                params,
            )
            has_more = len(operations) > (query.limit or 25)
            operations = operations[: query.limit]
            budgets = _rows(connection, _BUDGET_SQL, {})
            return ReadSnapshot(
                empty,
                empty,
                empty,
                operations,
                empty,
                revision,
                budgets,
                scoped_page=True,
                has_more=has_more,
            )

        if query.endpoint == "coverage":
            clauses = []
            params = {}
            _append_filter(clauses, params, "scoped.data_kind", "data_kind", filters)
            _append_filter(clauses, params, "scoped.availability", "availability", filters)
            coverage = _rows(connection, _scoped_sql(_COVERAGE_SQL, clauses), params)
            return ReadSnapshot(empty, empty, empty, empty, coverage, revision)

        return self.load()

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
           'finality', latest_result.finality) END AS result
FROM mirror.matches m
JOIN mirror.competitions c ON c.id = m.competition_id
JOIN latest ON latest.match_id = m.id
JOIN mirror.team_identities home ON home.id = latest.home_team_id
JOIN mirror.team_identities away ON away.id = latest.away_team_id
LEFT JOIN mirror.venues venue ON venue.id = latest.venue_id
LEFT JOIN latest_result ON latest_result.match_id = m.id
"""

_PREDICTION_SQL = """
SELECT p.id::text, 'prediction'::text AS record_type,
       p.id::text AS prediction_revision_id, NULL::text AS attempt_id,
       p.match_id::text, c.source_competition_code AS competition,
       c.division, c.stage AS competition_stage, v.provider, v.id::text AS variant_id,
       p.stage AS prediction_type, v.requested_model,
       p.resolved_model_id, v.pinned_model_version AS model_version,
       v.prompt_version, v.feature_version, s.availability_policy,
       p.schedule_revision_id::text,
       s.id::text AS feature_snapshot_id, mr.raw_snapshot_id::text AS source_snapshot_id,
       mr.home_team_id::text, prediction_home.source_team_code AS home_team_code,
       mr.away_team_id::text, prediction_away.source_team_code AS away_team_code,
       p.input_cutoff_at, p.generated_at,
       coalesce(ps.current_status, 'published') AS status,
       coalesce(ps.current_status, 'published') AS lifecycle_status,
       'succeeded'::text AS provider_status, NULL::text AS error_code,
       market_evaluation.eligibility AS market_eligibility,
       market_evaluation.reason AS market_reason,
       market_snapshot.id::text AS market_snapshot_id,
       market_snapshot.source AS market_source,
       market_snapshot.quoted_at AS market_quoted_at,
       p.output_json AS output
FROM engine.predictions p
JOIN engine.model_variants v ON v.id = p.variant_id
JOIN mirror.matches m ON m.id = p.match_id
JOIN mirror.competitions c ON c.id = m.competition_id
JOIN engine.feature_snapshots s ON s.id = p.snapshot_id
JOIN mirror.match_revisions mr ON mr.id = p.schedule_revision_id
JOIN mirror.team_identities prediction_home ON prediction_home.id = mr.home_team_id
JOIN mirror.team_identities prediction_away ON prediction_away.id = mr.away_team_id
LEFT JOIN engine.prediction_status_projection ps ON ps.prediction_id = p.id
LEFT JOIN LATERAL (
    SELECT evaluation.market_snapshot_id, evaluation.eligibility, evaluation.reason
    FROM market.market_evaluations AS evaluation
    WHERE evaluation.prediction_id = p.id
    ORDER BY evaluation.created_at DESC, evaluation.id DESC
    LIMIT 1
) AS market_evaluation ON true
LEFT JOIN market.market_snapshots AS market_snapshot
    ON market_snapshot.id = coalesce(market_evaluation.market_snapshot_id, p.market_snapshot_id)
UNION ALL
SELECT a.id::text, 'attempt'::text AS record_type,
       NULL::text AS prediction_revision_id, a.id::text AS attempt_id,
       s.match_id::text, c.source_competition_code AS competition,
       c.division, c.stage AS competition_stage, v.provider, v.id::text AS variant_id,
       coalesce(j.stage, 'provider_attempt') AS prediction_type,
       v.requested_model, NULL::text AS resolved_model_id,
       v.pinned_model_version AS model_version, v.prompt_version, v.feature_version,
       s.availability_policy,
       s.schedule_revision_id::text, s.id::text AS feature_snapshot_id,
       mr.raw_snapshot_id::text AS source_snapshot_id,
       mr.home_team_id::text, prediction_home.source_team_code AS home_team_code,
       mr.away_team_id::text, prediction_away.source_team_code AS away_team_code,
       s.cutoff_at AS input_cutoff_at,
       coalesce(a.completed_at, a.started_at) AS generated_at,
       a.status, NULL::text AS lifecycle_status,
       a.status AS provider_status, a.error_code,
       NULL::text AS market_eligibility, NULL::text AS market_reason,
       NULL::text AS market_snapshot_id, NULL::text AS market_source,
       NULL::timestamptz AS market_quoted_at,
       '{}'::jsonb AS output
FROM engine.prediction_attempts a
JOIN engine.feature_snapshots s ON s.id = a.snapshot_id
JOIN engine.model_variants v ON v.id = a.variant_id
JOIN mirror.matches m ON m.id = s.match_id
JOIN mirror.competitions c ON c.id = m.competition_id
JOIN mirror.match_revisions mr ON mr.id = s.schedule_revision_id
JOIN mirror.team_identities prediction_home ON prediction_home.id = mr.home_team_id
JOIN mirror.team_identities prediction_away ON prediction_away.id = mr.away_team_id
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
    SELECT eligibility, market_snapshot_id FROM market.market_evaluations
    WHERE prediction_id = p.id ORDER BY created_at DESC LIMIT 1
) me ON true
LEFT JOIN market.market_snapshots market_snapshot ON market_snapshot.id = me.market_snapshot_id
"""

_OPERATION_SQL = """
SELECT id::text, job_type, state, due_at, deadline_at, attempt_no, error_code,
       schedule_revision_id::text, match_id::text, updated_at
FROM ops.jobs
"""

_COVERAGE_SQL = """
SELECT DISTINCT ON (source, season_id, competition_id, match_id, data_kind)
       id::text, source, season_id::text, competition_id::text, match_id::text,
       data_kind, availability, observed_at,
       split_part(evidence, ' ', 1) AS evidence_code,
       raw_snapshot_id::text AS source_snapshot_id
FROM mirror.source_coverage
ORDER BY source, season_id, competition_id, match_id, data_kind,
         observed_at DESC, created_at DESC, id DESC
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


def _revision_sql(*components: str) -> str:
    values = ",\n".join(f"    coalesce(({component}), '0:')" for component in components)
    return f"SELECT md5(concat_ws('|',\n{values}\n))"


_MATCH_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(created_at)::text FROM mirror.match_revisions"
)
_RESULT_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(created_at)::text FROM mirror.result_revisions"
)
_PREDICTION_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(created_at)::text FROM engine.predictions"
)
_ATTEMPT_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || "
    "max(greatest(created_at, coalesce(completed_at, created_at)))::text "
    "FROM engine.prediction_attempts"
)
_PREDICTION_EVENT_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(created_at)::text FROM engine.prediction_events"
)
_PREDICTION_STATUS_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(refreshed_at)::text "
    "FROM engine.prediction_status_projection"
)
_EVALUATION_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(created_at)::text FROM engine.evaluations"
)
_MARKET_SNAPSHOT_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(created_at)::text FROM market.market_snapshots"
)
_MARKET_EVALUATION_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(created_at)::text FROM market.market_evaluations"
)
_BUDGET_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(coalesce(settled_at, created_at))::text "
    "FROM ops.provider_budget_reservations"
)
_JOB_REVISION_COMPONENT = "SELECT count(*)::text || ':' || max(updated_at)::text FROM ops.jobs"
_COVERAGE_REVISION_COMPONENT = (
    "SELECT count(*)::text || ':' || max(created_at)::text FROM mirror.source_coverage"
)

_PREDICTION_COMPONENTS = (
    _PREDICTION_REVISION_COMPONENT,
    _ATTEMPT_REVISION_COMPONENT,
    _PREDICTION_EVENT_REVISION_COMPONENT,
    _PREDICTION_STATUS_REVISION_COMPONENT,
)
_MARKET_COMPONENTS = (
    _MARKET_SNAPSHOT_REVISION_COMPONENT,
    _MARKET_EVALUATION_REVISION_COMPONENT,
)
_REVISION_SQL = _revision_sql(
    _MATCH_REVISION_COMPONENT,
    _RESULT_REVISION_COMPONENT,
    *_PREDICTION_COMPONENTS,
    _EVALUATION_REVISION_COMPONENT,
    *_MARKET_COMPONENTS,
    _BUDGET_REVISION_COMPONENT,
    _JOB_REVISION_COMPONENT,
    _COVERAGE_REVISION_COMPONENT,
)
_ENDPOINT_REVISION_SQL = {
    "schedule": _revision_sql(
        _MATCH_REVISION_COMPONENT,
        _RESULT_REVISION_COMPONENT,
        *_PREDICTION_COMPONENTS,
        *_MARKET_COMPONENTS,
    ),
    "match": _revision_sql(
        _MATCH_REVISION_COMPONENT,
        _RESULT_REVISION_COMPONENT,
        *_PREDICTION_COMPONENTS,
        *_MARKET_COMPONENTS,
        _COVERAGE_REVISION_COMPONENT,
    ),
    "predictions": _revision_sql(
        _MATCH_REVISION_COMPONENT,
        *_PREDICTION_COMPONENTS,
        _EVALUATION_REVISION_COMPONENT,
    ),
    "performance": _revision_sql(
        _MATCH_REVISION_COMPONENT,
        _RESULT_REVISION_COMPONENT,
        *_PREDICTION_COMPONENTS,
        _EVALUATION_REVISION_COMPONENT,
        *_MARKET_COMPONENTS,
    ),
    "operations": _revision_sql(_JOB_REVISION_COMPONENT, _BUDGET_REVISION_COMPONENT),
    "coverage": _revision_sql(_COVERAGE_REVISION_COMPONENT),
}


def _append_filter(
    clauses: list[str],
    params: dict[str, object],
    column: str,
    name: str,
    filters: Mapping[str, object],
) -> None:
    value = filters.get(name)
    if value is not None:
        clauses.append(f"{column} = :{name}")
        params[name] = value


def _scoped_sql(base_sql: str, clauses: list[str], suffix: str = "") -> str:
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return f"SELECT * FROM ({base_sql}) AS scoped{where} {suffix}"


def _rows(
    connection: Any, statement: str, params: Mapping[str, object]
) -> tuple[Mapping[str, Any], ...]:
    return tuple(dict(row) for row in connection.execute(text(statement), dict(params)).mappings())


def object_mapping(value: object) -> dict[str, object]:
    """Normalize JSONB driver values without accepting unexpected scalar payloads."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): cast(object, item) for key, item in value.items()}


def aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be a timezone-aware datetime")
    return value
