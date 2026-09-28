"""Benchmark scoped API reads against a disposable migrated PostgreSQL database."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import re
import sys
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from uuid import UUID

import psycopg
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, text

from vlytics.api.app import create_app
from vlytics.api.repository import PostgresReadRepository, ReadQuery, ReadSnapshot

SCHEMA_VERSION = "api-read-benchmark-v1"
SOURCE = "synthetic-api-benchmark-v1"
COMPETITION = "benchmark-main"
JOB_TYPE = "benchmark.api-read"
COVERAGE_KIND = "benchmark-read"
EVALUATOR = "benchmark-evaluator-v1"
PROVIDERS = ("openai", "anthropic", "gemini")
SEASONS = 22
MATCHES_PER_SEASON = 250
MATCHES = SEASONS * MATCHES_PER_SEASON
PREDICTIONS = MATCHES * len(PROVIDERS)
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,127}")
ROW_FIELDS = (
    "matches",
    "predictions",
    "evaluations",
    "operations",
    "coverage",
    "budgets",
)
EXPECTED_ROWS: dict[str, dict[str, int | bool]] = {
    "schedule": {"matches": 1, "predictions": 3},
    "match": {"matches": 1, "predictions": 3, "coverage": 1},
    "predictions": {
        "matches": 25,
        "predictions": 25,
        "evaluations": 25,
        "has_more": True,
    },
    "performance": {"evaluations": PREDICTIONS},
    "performance_recent_season": {"evaluations": 750},
    "operations": {"operations": 25, "budgets": 6, "has_more": True},
    "coverage": {"coverage": MATCHES},
    "legacy_full_load": {
        "matches": MATCHES,
        "predictions": PREDICTIONS,
        "evaluations": PREDICTIONS,
        "operations": MATCHES,
        "coverage": MATCHES,
        "budgets": 6,
    },
}

EXPECTED_API: dict[str, dict[str, int | bool]] = {
    "schedule": {
        "status_code": 200,
        "items": 1,
        "has_more": False,
        "sample_size_sum": 0,
        "evaluation_revision_sum": 0,
        "calibration_sample_sum": 0,
    },
    "history_first_page": {
        "status_code": 200,
        "items": 25,
        "has_more": True,
        "sample_size_sum": 0,
        "evaluation_revision_sum": 0,
        "calibration_sample_sum": 0,
    },
    "history_second_page": {
        "status_code": 200,
        "items": 25,
        "has_more": True,
        "sample_size_sum": 0,
        "evaluation_revision_sum": 0,
        "calibration_sample_sum": 0,
    },
    "performance_full_cohort": {
        "status_code": 200,
        "items": 12,
        "has_more": False,
        "sample_size_sum": PREDICTIONS,
        "evaluation_revision_sum": PREDICTIONS,
        "calibration_sample_sum": PREDICTIONS,
    },
    "performance_recent_season": {
        "status_code": 200,
        "items": 6,
        "has_more": False,
        "sample_size_sum": 750,
        "evaluation_revision_sum": 750,
        "calibration_sample_sum": 750,
    },
    "operations": {
        "status_code": 200,
        "items": 25,
        "has_more": True,
        "sample_size_sum": 0,
        "evaluation_revision_sum": 0,
        "calibration_sample_sum": 0,
    },
    "coverage": {
        "status_code": 200,
        "items": 1,
        "has_more": False,
        "sample_size_sum": 0,
        "evaluation_revision_sum": 0,
        "calibration_sample_sum": 0,
    },
}


SEED_SQL = r"""
INSERT INTO mirror.raw_snapshots (
    id, source, source_group_code, request_fingerprint, redacted_url,
    requested_at, received_at, status_code, parser_version
) VALUES (
    md5('benchmark-raw')::uuid, :source, 'benchmark', 'benchmark-fixture',
    'synthetic://api-benchmark', '1999-12-31T00:00:00Z',
    '1999-12-31T00:00:00Z', 200, 'benchmark-v1'
);

INSERT INTO mirror.seasons (
    id, source, source_group_code, source_season_code, label,
    starts_at, ends_at, observed_at, raw_snapshot_id
)
SELECT md5('benchmark-season-' || season_no)::uuid, :source, 'benchmark',
       'benchmark-' || season_no, 'Benchmark season ' || season_no,
       make_timestamptz(2000 + season_no, 1, 1, 0, 0, 0, 'UTC'),
       make_timestamptz(2000 + season_no, 12, 31, 23, 59, 59, 'UTC'),
       '1999-12-31T00:00:00Z', md5('benchmark-raw')::uuid
FROM generate_series(0, :season_count - 1) AS season_no;

INSERT INTO mirror.competitions (
    id, source, source_group_code, season_id, source_competition_code,
    division, stage, label, mapping_version, observed_at, raw_snapshot_id
)
SELECT md5('benchmark-competition-' || season_no)::uuid, :source, 'benchmark',
       md5('benchmark-season-' || season_no)::uuid, :competition,
       CASE WHEN season_no % 2 = 0 THEN 'women' ELSE 'men' END,
       'regular', 'Benchmark competition', 'benchmark-v1',
       '1999-12-31T00:00:00Z', md5('benchmark-raw')::uuid
FROM generate_series(0, :season_count - 1) AS season_no;

INSERT INTO mirror.team_identities (
    id, source, source_team_code, season_id, display_name,
    observed_at, mapping_version, raw_snapshot_id
)
SELECT md5('benchmark-team-' || season_no || '-' || side)::uuid, :source,
       'benchmark-' || season_no || '-' || side,
       md5('benchmark-season-' || season_no)::uuid,
       'Benchmark ' || side || ' ' || season_no,
       '1999-12-31T00:00:00Z', 'benchmark-v1', md5('benchmark-raw')::uuid
FROM generate_series(0, :season_count - 1) AS season_no
CROSS JOIN (VALUES ('home'), ('away')) AS sides(side);

INSERT INTO mirror.matches (
    id, source, source_group_code, source_season_code,
    source_competition_code, source_match_code, season_id,
    competition_id, first_observed_at, raw_snapshot_id
)
SELECT md5('benchmark-match-' || season_no || '-' || match_no)::uuid,
       :source, 'benchmark', 'benchmark-' || season_no, :competition,
       'benchmark-' || season_no || '-' || match_no,
       md5('benchmark-season-' || season_no)::uuid,
       md5('benchmark-competition-' || season_no)::uuid,
       '1999-12-31T00:00:00Z', md5('benchmark-raw')::uuid
FROM generate_series(0, :season_count - 1) AS season_no
CROSS JOIN generate_series(1, :matches_per_season) AS match_no;

INSERT INTO mirror.match_revisions (
    id, match_id, revision, home_team_id, away_team_id,
    scheduled_start_at, status, observed_at, raw_snapshot_id
)
SELECT md5('benchmark-revision-' || season_no || '-' || match_no)::uuid,
       md5('benchmark-match-' || season_no || '-' || match_no)::uuid, 1,
       md5('benchmark-team-' || season_no || '-home')::uuid,
       md5('benchmark-team-' || season_no || '-away')::uuid,
       make_timestamptz(2000 + season_no, 1, 1, 12, 0, 0, 'UTC')
           + (match_no - 1) * interval '1 day',
       'finished',
       make_timestamptz(2000 + season_no, 1, 1, 15, 0, 0, 'UTC')
           + (match_no - 1) * interval '1 day',
       md5('benchmark-raw')::uuid
FROM generate_series(0, :season_count - 1) AS season_no
CROSS JOIN generate_series(1, :matches_per_season) AS match_no;

INSERT INTO mirror.result_revisions (
    id, match_id, revision, home_sets, away_sets, home_points,
    away_points, finality, observed_at, raw_snapshot_id, rule_version
)
SELECT md5('benchmark-result-' || season_no || '-' || match_no)::uuid,
       md5('benchmark-match-' || season_no || '-' || match_no)::uuid, 1,
       3, 1, 96, 82, 'final',
       make_timestamptz(2000 + season_no, 1, 1, 15, 0, 0, 'UTC')
           + (match_no - 1) * interval '1 day',
       md5('benchmark-raw')::uuid, 'benchmark-v1'
FROM generate_series(0, :season_count - 1) AS season_no
CROSS JOIN generate_series(1, :matches_per_season) AS match_no;

INSERT INTO engine.model_variants (
    id, provider, requested_model, pinned_model_version, prompt_version,
    prompt_hash, feature_version, output_schema_version, hyperparameters
)
SELECT md5('benchmark-variant-' || provider)::uuid, provider,
       'benchmark-' || provider, 'benchmark-2026-09', 'benchmark-prompt-v1',
       repeat(substr(md5(provider), 1, 1), 64), 'benchmark-feature-v1',
       'benchmark-output-v1', '{}'::jsonb
FROM unnest(CAST(:providers AS text[])) AS provider;

INSERT INTO engine.feature_snapshots (
    id, match_id, schedule_revision_id, cutoff_at, captured_at,
    feature_version, availability_policy, lineup_status,
    values_json, lineage_json, sha256
)
SELECT md5('benchmark-snapshot-' || season_no || '-' || match_no)::uuid,
       md5('benchmark-match-' || season_no || '-' || match_no)::uuid,
       md5('benchmark-revision-' || season_no || '-' || match_no)::uuid,
       make_timestamptz(2000 + season_no, 1, 1, 11, 0, 0, 'UTC')
           + (match_no - 1) * interval '1 day',
       make_timestamptz(2000 + season_no, 1, 1, 11, 0, 0, 'UTC')
           + (match_no - 1) * interval '1 day',
       'benchmark-feature-v1', 'historical_point_in_time', 'unknown',
       jsonb_build_object('benchmark', true), jsonb_build_object('source', 'synthetic'),
       repeat(substr(md5(season_no::text || '-' || match_no), 1, 1), 64)
FROM generate_series(0, :season_count - 1) AS season_no
CROSS JOIN generate_series(1, :matches_per_season) AS match_no;

INSERT INTO engine.predictions (
    id, match_id, schedule_revision_id, snapshot_id, variant_id, stage,
    input_cutoff_at, started_at, generated_at, resolved_model_id,
    output_json, sha256
)
SELECT md5('benchmark-prediction-' || season_no || '-' || match_no || '-' || provider)::uuid,
       md5('benchmark-match-' || season_no || '-' || match_no)::uuid,
       md5('benchmark-revision-' || season_no || '-' || match_no)::uuid,
       md5('benchmark-snapshot-' || season_no || '-' || match_no)::uuid,
       md5('benchmark-variant-' || provider)::uuid, 'pre_match',
       scheduled_at - interval '60 minutes',
       scheduled_at - interval '59 minutes 55 seconds',
       scheduled_at - interval '59 minutes 50 seconds', 'benchmark-' || provider || '-resolved',
       jsonb_build_object('home_win_probability', 0.55, 'away_win_probability', 0.45),
       repeat(substr(md5(provider || season_no::text || match_no::text), 1, 1), 64)
FROM generate_series(0, :season_count - 1) AS season_no
CROSS JOIN generate_series(1, :matches_per_season) AS match_no
CROSS JOIN unnest(CAST(:providers AS text[])) AS provider
CROSS JOIN LATERAL (
    SELECT make_timestamptz(2000 + season_no, 1, 1, 12, 0, 0, 'UTC')
        + (match_no - 1) * interval '1 day' AS scheduled_at
) AS timing;

INSERT INTO engine.prediction_events (
    id, prediction_id, event_type, reason, occurred_at, observed_at, schedule_revision_id
)
SELECT md5('benchmark-published-' || p.id::text)::uuid, p.id, 'published',
       'synthetic benchmark publication', p.generated_at, p.generated_at,
       p.schedule_revision_id
FROM engine.predictions p;

INSERT INTO engine.prediction_status_projection (
    prediction_id, latest_event_id, current_status, refreshed_at
)
SELECT prediction_id, id, event_type, observed_at
FROM engine.prediction_events;

INSERT INTO engine.evaluations (
    id, match_id, prediction_id, result_revision_id, evaluator_version,
    cohort_policy_version, metric_values, settlement
)
SELECT md5('benchmark-evaluation-' || season_no || '-' || match_no || '-' || provider)::uuid,
       md5('benchmark-match-' || season_no || '-' || match_no)::uuid,
       md5('benchmark-prediction-' || season_no || '-' || match_no || '-' || provider)::uuid,
       md5('benchmark-result-' || season_no || '-' || match_no)::uuid,
       :evaluator, 'benchmark-cohort-v1',
       jsonb_build_object(
           'eligible', true, 'winner_accuracy', 1.0, 'brier', 0.2025,
           'log_loss', 0.5978, 'home_win_probability', 0.55,
           'home_win_outcome', 1,
           'cohort', jsonb_build_object(
               'stage', 'regular',
               'feature_version', 'benchmark-feature-v1',
               'availability_policy', 'historical_point_in_time',
               'timing_eligibility', 'on_time',
               'result_finality', 'final'
           )
       ),
       jsonb_build_object('status', 'settled')
FROM generate_series(0, :season_count - 1) AS season_no
CROSS JOIN generate_series(1, :matches_per_season) AS match_no
CROSS JOIN unnest(CAST(:providers AS text[])) AS provider;

INSERT INTO ops.jobs (id, job_key, job_type, due_at, state)
SELECT md5('benchmark-job-' || row_no)::uuid, 'benchmark-job-' || row_no,
       :job_type, '2026-09-27T00:00:00Z'::timestamptz + row_no * interval '1 second',
       CASE WHEN row_no % 2 = 0 THEN 'failed' ELSE 'succeeded' END
FROM generate_series(1, :match_count) AS row_no;

INSERT INTO mirror.source_coverage (
    id, source, season_id, competition_id, match_id, data_kind,
    availability, evidence, observed_at, raw_snapshot_id
)
SELECT md5('benchmark-coverage-' || season_no || '-' || match_no)::uuid,
       :source, md5('benchmark-season-' || season_no)::uuid,
       md5('benchmark-competition-' || season_no)::uuid,
       md5('benchmark-match-' || season_no || '-' || match_no)::uuid,
       :coverage_kind, 'available', 'synthetic benchmark',
       make_timestamptz(2000 + season_no, 1, 1, 10, 0, 0, 'UTC')
           + (match_no - 1) * interval '1 day',
       md5('benchmark-raw')::uuid
FROM generate_series(0, :season_count - 1) AS season_no
CROSS JOIN generate_series(1, :matches_per_season) AS match_no;

INSERT INTO ops.provider_budget_reservations (
    id, reservation_key, provider, job_id, reserved_at, budget_day,
    budget_month, reserved_amount, settled_amount, state,
    conservative_charge, settled_at, currency
)
SELECT md5('benchmark-budget-' || row_no)::uuid, 'benchmark-budget-' || row_no,
       provider, md5('benchmark-job-' || row_no)::uuid,
       '2026-09-27T00:00:00Z'::timestamptz + row_no * interval '1 second',
       '2026-09-27'::date, '2026-09-01'::date, 0.01, 0.01,
       'settled', false,
       '2026-09-27T00:00:00Z'::timestamptz + row_no * interval '1 second', 'USD'
FROM generate_series(1, :match_count) AS row_no
CROSS JOIN LATERAL (
    SELECT (CAST(:providers AS text[]))[
        1 + ((row_no - 1) % cardinality(CAST(:providers AS text[])))
    ] AS provider
) AS selected;
"""


def _env_value(name: str) -> str:
    if ENV_NAME.fullmatch(name) is None:
        raise ValueError("invalid environment variable name")
    value = os.environ.get(name)
    if not value:
        raise ValueError("required database environment variable is missing")
    return value


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 3),
        "p50": round(median(values), 3),
        "p95_nearest_rank": round(_nearest_rank(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def _rows(snapshot: ReadSnapshot) -> dict[str, int | bool]:
    return {
        "matches": len(snapshot.matches),
        "predictions": len(snapshot.predictions),
        "evaluations": len(snapshot.evaluations),
        "operations": len(snapshot.operations),
        "coverage": len(snapshot.coverage),
        "budgets": len(snapshot.budgets),
        "has_more": snapshot.has_more,
    }


def _expanded_expected_rows(name: str) -> dict[str, int | bool]:
    expected: dict[str, int | bool] = {field: 0 for field in ROW_FIELDS}
    expected["has_more"] = False
    expected.update(EXPECTED_ROWS[name])
    return expected


def _seed(engine: Engine) -> dict[str, int]:
    params = {
        "source": SOURCE,
        "competition": COMPETITION,
        "job_type": JOB_TYPE,
        "coverage_kind": COVERAGE_KIND,
        "evaluator": EVALUATOR,
        "providers": list(PROVIDERS),
        "season_count": SEASONS,
        "matches_per_season": MATCHES_PER_SEASON,
        "match_count": MATCHES,
    }
    with engine.begin() as connection:
        existing = connection.execute(
            text(
                """SELECT
                    (SELECT count(*) FROM mirror.matches)
                  + (SELECT count(*) FROM engine.predictions)
                  + (SELECT count(*) FROM engine.evaluations)
                  + (SELECT count(*) FROM ops.jobs)
                  + (SELECT count(*) FROM mirror.source_coverage)
                  + (SELECT count(*) FROM ops.provider_budget_reservations)
                """
            )
        ).scalar_one()
        if existing:
            raise RuntimeError("benchmark requires a disposable blank migrated database")
        for statement in SEED_SQL.strip().split(";\n\n"):
            connection.execute(text(statement), params)
    return _fixture_counts(engine)


def _analyze_fixture(engine: Engine) -> None:
    tables = (
        "mirror.matches",
        "mirror.match_revisions",
        "mirror.result_revisions",
        "mirror.source_coverage",
        "engine.feature_snapshots",
        "engine.predictions",
        "engine.prediction_events",
        "engine.prediction_status_projection",
        "engine.evaluations",
        "ops.jobs",
        "ops.provider_budget_reservations",
    )
    with engine.begin() as connection:
        for table in tables:
            connection.exec_driver_sql(f"ANALYZE {table}")


def _fixture_counts(engine: Engine) -> dict[str, int]:
    statements = {
        "matches": "SELECT count(*) FROM mirror.matches WHERE source = :source",
        "predictions": """SELECT count(*) FROM engine.predictions p
            JOIN mirror.matches m ON m.id = p.match_id WHERE m.source = :source""",
        "prediction_events": """SELECT count(*) FROM engine.prediction_events e
            JOIN engine.predictions p ON p.id = e.prediction_id
            JOIN mirror.matches m ON m.id = p.match_id WHERE m.source = :source""",
        "prediction_projections": """SELECT count(*) FROM engine.prediction_status_projection ps
            JOIN engine.predictions p ON p.id = ps.prediction_id
            JOIN mirror.matches m ON m.id = p.match_id WHERE m.source = :source""",
        "evaluations": """SELECT count(*) FROM engine.evaluations e
            JOIN mirror.matches m ON m.id = e.match_id WHERE m.source = :source""",
        "operations": "SELECT count(*) FROM ops.jobs WHERE job_type = :job_type",
        "coverage": "SELECT count(*) FROM mirror.source_coverage WHERE source = :source",
        "budgets": """SELECT count(*) FROM ops.provider_budget_reservations
            WHERE reservation_key LIKE 'benchmark-budget-%'""",
    }
    params = {"source": SOURCE, "job_type": JOB_TYPE}
    with engine.connect() as connection:
        return {
            name: int(connection.execute(text(statement), params).scalar_one())
            for name, statement in statements.items()
        }


def _queries() -> dict[str, ReadQuery | None]:
    return {
        "schedule": ReadQuery(
            endpoint="schedule",
            filters={
                "start_at": datetime(2000, 1, 1, tzinfo=UTC),
                "end_at": datetime(2000, 1, 2, tzinfo=UTC),
                "division": "women",
                "competition": COMPETITION,
                "team": None,
            },
        ),
        "match": ReadQuery(
            endpoint="match",
            filters={"match_id": str(UUID(hashlib.md5(b"benchmark-match-0-1").hexdigest()))},
        ),
        "predictions": ReadQuery(
            endpoint="predictions",
            filters={
                "division": None,
                "competition": COMPETITION,
                "provider": "openai",
                "model": None,
                "prediction_type": None,
                "prompt_version": "benchmark-prompt-v1",
                "team": None,
                "start_at": None,
                "end_at": None,
            },
            limit=25,
        ),
        "performance": ReadQuery(
            endpoint="performance",
            filters={
                "division": None,
                "competition": COMPETITION,
                "stage": None,
                "feature_version": "benchmark-feature-v1",
                "availability_policy": None,
                "timing_eligibility": None,
                "evaluator_version": EVALUATOR,
                "result_finality": "final",
                "provider": None,
                "model": None,
                "prompt_version": None,
                "prediction_type": None,
                "start_at": None,
                "end_at": None,
            },
        ),
        "performance_recent_season": ReadQuery(
            endpoint="performance",
            filters={
                "division": None,
                "competition": COMPETITION,
                "stage": None,
                "feature_version": "benchmark-feature-v1",
                "availability_policy": None,
                "timing_eligibility": None,
                "evaluator_version": EVALUATOR,
                "result_finality": "final",
                "provider": None,
                "model": None,
                "prompt_version": None,
                "prediction_type": None,
                "start_at": datetime(2021, 1, 1, tzinfo=UTC),
                "end_at": datetime(2022, 1, 1, tzinfo=UTC),
            },
        ),
        "operations": ReadQuery(
            endpoint="operations",
            filters={"state": None, "job_type": JOB_TYPE},
            limit=25,
        ),
        "coverage": ReadQuery(
            endpoint="coverage",
            filters={"data_kind": COVERAGE_KIND, "availability": "available"},
        ),
        "legacy_full_load": None,
    }


def _measure_latency(
    query_counter: list[int], operation: Callable[[], ReadSnapshot]
) -> dict[str, Any]:
    gc.collect()
    query_counter[0] = 0
    started = perf_counter()
    snapshot = operation()
    duration_ms = (perf_counter() - started) * 1000
    result = {
        "duration_ms": round(duration_ms, 3),
        "sql_statements": query_counter[0],
        "rows": _rows(snapshot),
    }
    del snapshot
    return result


def _measure_memory(operation: Callable[[], ReadSnapshot]) -> dict[str, Any]:
    gc.collect()
    tracemalloc.start()
    try:
        before_current, _ = tracemalloc.get_traced_memory()
        snapshot = operation()
        _, peak = tracemalloc.get_traced_memory()
        result = {
            "client_peak_mib": round(max(0, peak - before_current) / (1024 * 1024), 3),
            "rows": _rows(snapshot),
        }
        del snapshot
        return result
    finally:
        tracemalloc.stop()


def _benchmark(read_url: str, warm_samples: int) -> dict[str, Any]:
    engine = create_engine(read_url, pool_pre_ping=True)
    query_counter = [0]

    @event.listens_for(engine, "before_cursor_execute")
    def _count_query(*_: object) -> None:
        query_counter[0] += 1

    repository = PostgresReadRepository(engine)
    measurements: dict[str, Any] = {}
    try:
        for name, query in _queries().items():
            operation: Callable[[], ReadSnapshot] = (
                repository.load if query is None else partial(repository.load_query, query)
            )

            engine.dispose()
            cold = _measure_latency(query_counter, operation)
            warm = [_measure_latency(query_counter, operation) for _ in range(warm_samples)]
            memory = _measure_memory(operation)
            row_shapes = [sample["rows"] for sample in warm]
            if any(rows != cold["rows"] for rows in row_shapes) or memory["rows"] != cold["rows"]:
                raise RuntimeError("benchmark row cardinality changed between samples")
            if cold["rows"] != _expanded_expected_rows(name):
                raise RuntimeError("benchmark query cardinality mismatch")
            measurements[name] = {
                "cold_client": cold,
                "warm": {
                    "samples": warm_samples,
                    "duration_ms": _summary([sample["duration_ms"] for sample in warm]),
                    "sql_statements": sorted({sample["sql_statements"] for sample in warm}),
                    "rows": cold["rows"],
                },
                "memory_pass": memory,
            }
    finally:
        engine.dispose()
    return measurements


def _http_shape(response: Any) -> dict[str, int | bool]:
    if response.status_code != 200:
        raise RuntimeError("benchmark API response was not successful")
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("benchmark API response has no data object")
    items = data.get("items")
    if not isinstance(items, list):
        raise RuntimeError("benchmark API response has no items")
    return {
        "status_code": response.status_code,
        "items": len(items),
        "has_more": data.get("next_cursor") is not None,
        "sample_size_sum": sum(
            item.get("sample_size", 0)
            for item in items
            if isinstance(item, dict) and isinstance(item.get("sample_size", 0), int)
        ),
        "evaluation_revision_sum": sum(
            len(item.get("evaluation_revision_ids", []))
            for item in items
            if isinstance(item, dict) and isinstance(item.get("evaluation_revision_ids", []), list)
        ),
        "calibration_sample_sum": sum(
            bucket.get("sample_size", 0)
            for item in items
            if isinstance(item, dict) and isinstance(item.get("calibration", []), list)
            for bucket in item.get("calibration", [])
            if isinstance(bucket, dict) and isinstance(bucket.get("sample_size", 0), int)
        ),
    }


def _measure_http_latency(query_counter: list[int], operation: Callable[[], Any]) -> dict[str, Any]:
    gc.collect()
    query_counter[0] = 0
    started = perf_counter()
    response = operation()
    duration_ms = (perf_counter() - started) * 1000
    return {
        "duration_ms": round(duration_ms, 3),
        "sql_statements": query_counter[0],
        "response": _http_shape(response),
        "response_bytes": len(response.content),
    }


def _measure_http_memory(operation: Callable[[], Any]) -> dict[str, Any]:
    gc.collect()
    tracemalloc.start()
    try:
        before_current, _ = tracemalloc.get_traced_memory()
        response = operation()
        _, peak = tracemalloc.get_traced_memory()
        return {
            "client_peak_mib": round(max(0, peak - before_current) / (1024 * 1024), 3),
            "response": _http_shape(response),
            "response_bytes": len(response.content),
        }
    finally:
        tracemalloc.stop()


def _benchmark_api(read_url: str, warm_samples: int) -> dict[str, Any]:
    engine = create_engine(read_url, pool_pre_ping=True)
    query_counter = [0]

    @event.listens_for(engine, "before_cursor_execute")
    def _count_query(*_: object) -> None:
        query_counter[0] += 1

    repository = PostgresReadRepository(engine)
    app = create_app(
        repository=repository,
        environ={"VLYTICS_OPERATOR_AUTH_SECRET": "synthetic-benchmark-operator"},
    )
    headers = {"Authorization": "Bearer synthetic-benchmark-operator"}
    history_params = {
        "competition": COMPETITION,
        "provider": "openai",
        "prompt_version": "benchmark-prompt-v1",
        "limit": 25,
    }
    measurements: dict[str, Any] = {}
    try:
        with TestClient(app) as client:
            first = client.get("/api/v1/predictions", params=history_params, headers=headers)
            first_shape = _http_shape(first)
            history_cursor = first.json()["data"].get("next_cursor")
            if history_cursor is None or first_shape["items"] != 25:
                raise RuntimeError("benchmark History cursor precondition failed")
            specs: dict[str, Callable[[], Any]] = {
                "schedule": lambda: client.get(
                    "/api/v1/schedule",
                    params={
                        "date": "2000-01-01",
                        "timezone": "UTC",
                        "competition": COMPETITION,
                    },
                    headers=headers,
                ),
                "history_first_page": lambda: client.get(
                    "/api/v1/predictions", params=history_params, headers=headers
                ),
                "history_second_page": lambda: client.get(
                    "/api/v1/predictions",
                    params={**history_params, "cursor": history_cursor},
                    headers=headers,
                ),
                "performance_full_cohort": lambda: client.get(
                    "/api/v1/performance",
                    params={
                        "competition": COMPETITION,
                        "feature_version": "benchmark-feature-v1",
                        "evaluator_version": EVALUATOR,
                        "result_finality": "final",
                    },
                    headers=headers,
                ),
                "performance_recent_season": lambda: client.get(
                    "/api/v1/performance",
                    params={
                        "competition": COMPETITION,
                        "feature_version": "benchmark-feature-v1",
                        "evaluator_version": EVALUATOR,
                        "result_finality": "final",
                        "start_at": "2021-01-01T00:00:00Z",
                        "end_at": "2022-01-01T00:00:00Z",
                    },
                    headers=headers,
                ),
                "operations": lambda: client.get(
                    "/api/v1/operations",
                    params={"job_type": JOB_TYPE, "limit": 25},
                    headers=headers,
                ),
                "coverage": lambda: client.get(
                    "/api/v1/operations/coverage",
                    params={"data_kind": COVERAGE_KIND, "availability": "available"},
                    headers=headers,
                ),
            }
            for name, operation in specs.items():
                engine.dispose()
                cold = _measure_http_latency(query_counter, operation)
                warm = [
                    _measure_http_latency(query_counter, operation) for _ in range(warm_samples)
                ]
                memory = _measure_http_memory(operation)
                shapes = [sample["response"] for sample in warm]
                if (
                    any(shape != cold["response"] for shape in shapes)
                    or memory["response"] != cold["response"]
                ):
                    raise RuntimeError("benchmark API cardinality changed between samples")
                observed = {key: cold["response"][key] for key in EXPECTED_API[name]}
                if observed != EXPECTED_API[name]:
                    raise RuntimeError("benchmark API response cardinality mismatch")
                measurements[name] = {
                    "cold_client": cold,
                    "warm": {
                        "samples": warm_samples,
                        "duration_ms": _summary([sample["duration_ms"] for sample in warm]),
                        "sql_statements": sorted({sample["sql_statements"] for sample in warm}),
                        "response": cold["response"],
                        "response_bytes": sorted({sample["response_bytes"] for sample in warm}),
                    },
                    "memory_pass": memory,
                }
    finally:
        engine.dispose()
    return measurements


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrator-url-env", default="VLYTICS_BENCHMARK_MIGRATOR_DATABASE_URL")
    parser.add_argument("--read-url-env", default="VLYTICS_BENCHMARK_READ_API_DATABASE_URL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warm-samples", type=int, default=20)
    arguments = parser.parse_args()
    try:
        if arguments.warm_samples < 5 or arguments.warm_samples > 100:
            raise ValueError("warm samples must be between 5 and 100")
        migrator_url = _env_value(arguments.migrator_url_env)
        read_url = _env_value(arguments.read_url_env)
        migrator = create_engine(migrator_url, pool_pre_ping=True)
        started = perf_counter()
        try:
            counts = _seed(migrator)
            _analyze_fixture(migrator)
            with migrator.connect() as connection:
                server_version = str(connection.execute(text("SHOW server_version")).scalar_one())
        finally:
            migrator.dispose()
        expected = {
            "matches": MATCHES,
            "predictions": PREDICTIONS,
            "prediction_events": PREDICTIONS,
            "prediction_projections": PREDICTIONS,
            "evaluations": PREDICTIONS,
            "operations": MATCHES,
            "coverage": MATCHES,
            "budgets": MATCHES,
        }
        if counts != expected:
            raise RuntimeError("benchmark fixture cardinality mismatch")
        report = {
            "schema_version": SCHEMA_VERSION,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "fixture": {
                "seasons": SEASONS,
                "matches_per_season": MATCHES_PER_SEASON,
                "providers": list(PROVIDERS),
                "counts": counts,
                "seed_and_analyze_duration_ms": round((perf_counter() - started) * 1000, 3),
                "analyzed": True,
            },
            "code_sha256": {
                "benchmark_api_reads.py": _file_sha256(Path(__file__).resolve()),
                "repository.py": _file_sha256(
                    Path(__file__).resolve().parents[2]
                    / "backend"
                    / "src"
                    / "vlytics"
                    / "api"
                    / "repository.py"
                ),
            },
            "environment": {
                "python": platform.python_version(),
                "postgresql": server_version,
                "sqlalchemy": sqlalchemy.__version__,
                "psycopg": psycopg.__version__,
            },
            "method": {
                "source_hash_format": "UTF-8 source normalized to LF newlines",
                "cold_client": (
                    "first call after SQLAlchemy engine.dispose; "
                    "PostgreSQL shared buffers are not cleared"
                ),
                "warm_samples": arguments.warm_samples,
                "p95": "nearest-rank ceil(0.95 * n)",
                "memory": (
                    "Python tracemalloc incremental peak; excludes PostgreSQL, "
                    "driver native buffers, and process RSS"
                ),
            },
            "measurements": {
                "repository": _benchmark(read_url, arguments.warm_samples),
                "testclient": _benchmark_api(read_url, arguments.warm_samples),
            },
        }
        _write_json(arguments.output, report)
    except Exception:
        print(
            "API read benchmark failed; verify the disposable migrated database, "
            "role URLs, and output path.",
            file=sys.stderr,
        )
        return 1
    print(f"API read benchmark completed: {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
