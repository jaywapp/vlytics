"""PostgreSQL isolation checks for Market and result evaluation jobs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

import psycopg
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from test_immutability import _insert_mirror_graph

from vlytics.engine.evaluation import (
    COHORT_POLICY_VERSION,
    EVALUATION_JOB_TYPE,
    EVALUATOR_VERSION,
    EvaluationJobHandler,
    EvaluationJobPlanner,
    evaluation_job_key,
)
from vlytics.engine.market import (
    MarketComparisonJobPlanner,
    market_comparison_job_key,
    market_handlers,
)
from vlytics.ops.scheduler import PredictionJobDispatcher


class PostgresRoleUrls(Protocol):
    collector: str
    engine: str


def _conninfo(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def _digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prediction_output(variant_key: str) -> dict[str, Any]:
    return {
        "producer_variant_id": variant_key,
        "home_win_probability": 0.7,
        "set_score_probabilities": {
            "3:0": 0.2,
            "3:1": 0.2,
            "3:2": 0.3,
            "2:3": 0.1,
            "1:3": 0.1,
            "0:3": 0.1,
        },
    }


def _insert_prediction_pair(
    database_url: str,
    mirror_rows: dict[str, UUID],
) -> tuple[UUID, UUID]:
    now = datetime.now(UTC)
    feature_id = uuid4()
    good_variant_id = uuid4()
    bad_variant_id = uuid4()
    good_prediction_id = uuid4()
    bad_prediction_id = uuid4()
    good_variant_key = f"good-{uuid4().hex}"
    bad_variant_key = f"bad-{uuid4().hex}"
    good_output = _prediction_output(good_variant_key)
    bad_output = _prediction_output(bad_variant_key)

    with psycopg.connect(_conninfo(database_url)) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO engine.feature_snapshots (
                id, match_id, schedule_revision_id, cutoff_at, captured_at, feature_version,
                availability_policy, lineup_status, values_json, lineage_json, sha256
            ) VALUES (%s, %s, %s, %s, %s, 'evaluation-test-v1', 'live_prospective',
                      'unknown', '{}', '{}', %s)
            """,
            (
                feature_id,
                mirror_rows["match"],
                mirror_rows["fact"],
                now,
                now,
                "a" * 64,
            ),
        )
        for variant_id, variant_key in (
            (good_variant_id, good_variant_key),
            (bad_variant_id, bad_variant_key),
        ):
            cursor.execute(
                """
                INSERT INTO engine.model_variants (
                    id, provider, requested_model, pinned_model_version, prompt_version,
                    prompt_hash, feature_version, output_schema_version, hyperparameters
                ) VALUES (%s, 'statistical', %s, 'model-v1', 'prompt-v1', %s,
                          'evaluation-test-v1', 'prediction-v1', %s::jsonb)
                """,
                (
                    variant_id,
                    variant_key,
                    hashlib.sha256(variant_key.encode("utf-8")).hexdigest(),
                    json.dumps({"variant_key": variant_key}),
                ),
            )
        for prediction_id, variant_id, output, output_hash in (
            (good_prediction_id, good_variant_id, good_output, _digest(good_output)),
            (bad_prediction_id, bad_variant_id, bad_output, "0" * 64),
        ):
            cursor.execute(
                """
                INSERT INTO engine.predictions (
                    id, match_id, schedule_revision_id, snapshot_id, variant_id, stage,
                    input_cutoff_at, started_at, generated_at, resolved_model_id,
                    output_json, sha256
                ) VALUES (%s, %s, %s, %s, %s, 'pregame', %s, %s, %s,
                          'model-v1', %s::jsonb, %s)
                """,
                (
                    prediction_id,
                    mirror_rows["match"],
                    mirror_rows["fact"],
                    feature_id,
                    variant_id,
                    now,
                    now,
                    now,
                    json.dumps(output),
                    output_hash,
                ),
            )
            event_id = uuid4()
            cursor.execute(
                """
                INSERT INTO engine.prediction_events (
                    id, prediction_id, event_type, reason, occurred_at, observed_at,
                    schedule_revision_id
                ) VALUES (%s, %s, 'published', 'evaluation integration test', %s, %s, %s)
                """,
                (event_id, prediction_id, now, now, mirror_rows["fact"]),
            )
            cursor.execute(
                """
                INSERT INTO engine.prediction_status_projection (
                    prediction_id, latest_event_id, current_status, refreshed_at
                ) VALUES (%s, %s, 'published', %s)
                """,
                (prediction_id, event_id, now),
            )
        cursor.execute(
            """
            INSERT INTO market.market_evaluations (
                match_id, prediction_id, market_snapshot_id, evaluator_version,
                derived_probabilities, eligibility, reason
            ) VALUES (%s, %s, NULL, 'market-evaluator-v1', '{}', 'missing',
                      'no_market_evaluation_for_prediction')
            """,
            (mirror_rows["match"], good_prediction_id),
        )
    return good_prediction_id, bad_prediction_id


def _lease_job(
    engine: Engine,
    *,
    job_key: str,
    lease_owner: str,
    now: datetime,
) -> dict[str, Any]:
    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    """
                    UPDATE ops.jobs
                    SET state = 'running', lease_owner = :lease_owner,
                        lease_started_at = :now, lease_until = :lease_until,
                        attempt_no = attempt_no + 1, updated_at = :now
                    WHERE job_key = :job_key AND state = 'queued'
                    RETURNING *
                    """
                ),
                {
                    "job_key": job_key,
                    "lease_owner": lease_owner,
                    "now": now,
                    "lease_until": now + timedelta(minutes=2),
                },
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_bad_market_prediction_does_not_block_good_result_evaluation(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    mirror_rows = _insert_mirror_graph(postgres_role_urls.collector)
    good_prediction_id, bad_prediction_id = _insert_prediction_pair(
        postgres_role_urls.engine,
        mirror_rows,
    )
    engine = create_engine(postgres_role_urls.engine)
    now = datetime.now(UTC)
    market_key = market_comparison_job_key(bad_prediction_id)
    evaluation_key = evaluation_job_key(mirror_rows["result"], good_prediction_id)

    with engine.begin() as connection:
        MarketComparisonJobPlanner(connection).enqueue_pending(now=now, limit=1000)
        EvaluationJobPlanner(connection).enqueue_pending(now=now, limit=1000)
        planned_keys = set(
            connection.execute(
                text(
                    """
                    SELECT job_key
                    FROM ops.jobs
                    WHERE job_key IN (:market_key, :evaluation_key, :bad_evaluation_key)
                    """
                ),
                {
                    "market_key": market_key,
                    "evaluation_key": evaluation_key,
                    "bad_evaluation_key": evaluation_job_key(
                        mirror_rows["result"], bad_prediction_id
                    ),
                },
            ).scalars()
        )
    assert planned_keys == {market_key, evaluation_key}

    lease_owner = f"evaluation-integration-{uuid4()}"
    handlers = {
        **market_handlers(engine),
        EVALUATION_JOB_TYPE: EvaluationJobHandler(engine),
    }
    dispatcher = PredictionJobDispatcher(
        engine,
        handlers,
        lease_owner=lease_owner,
        now=lambda: now + timedelta(seconds=1),
    )
    dispatcher._execute(
        _lease_job(engine, job_key=market_key, lease_owner=lease_owner, now=now),
        now,
    )
    dispatcher._execute(
        _lease_job(engine, job_key=evaluation_key, lease_owner=lease_owner, now=now),
        now,
    )

    manual_key = evaluation_job_key(mirror_rows["result"])
    with engine.begin() as connection:
        EvaluationJobPlanner(connection).enqueue_result_revision(
            mirror_rows["result"],
            now=now,
        )
    dispatcher._execute(
        _lease_job(engine, job_key=manual_key, lease_owner=lease_owner, now=now),
        now,
    )

    with engine.connect() as connection:
        states = {
            str(row.job_key): (str(row.state), row.error_code)
            for row in connection.execute(
                text(
                    """
                    SELECT job_key, state, error_code
                    FROM ops.jobs
                    WHERE job_key IN (:market_key, :evaluation_key, :manual_key)
                    """
                ),
                {
                    "market_key": market_key,
                    "evaluation_key": evaluation_key,
                    "manual_key": manual_key,
                },
            )
        }
        evaluated_predictions = tuple(
            connection.execute(
                text(
                    """
                    SELECT prediction_id
                    FROM engine.evaluations
                    WHERE result_revision_id = :result_revision_id
                      AND evaluator_version = :evaluator_version
                      AND cohort_policy_version = :cohort_policy_version
                    ORDER BY prediction_id
                    """
                ),
                {
                    "result_revision_id": mirror_rows["result"],
                    "evaluator_version": EVALUATOR_VERSION,
                    "cohort_policy_version": COHORT_POLICY_VERSION,
                },
            ).scalars()
        )

    assert states == {
        market_key: ("quarantined", "ineligible_market_prediction"),
        evaluation_key: ("succeeded", None),
        manual_key: ("succeeded", None),
    }
    assert evaluated_predictions == (good_prediction_id,)
    engine.dispose()
