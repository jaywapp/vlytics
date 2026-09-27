"""Seed deterministic synthetic facts for the live browser smoke test.

The caller must provide a unique run id for every test run. The script writes facts only;
it does not enable source collection, a Market adapter, or any Provider call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from vlytics.engine.evaluation import (
    COHORT_POLICY_VERSION,
    EVALUATOR_VERSION,
    CohortKey,
    PredictionEvaluationInput,
    ResultEvaluator,
    ResultRevision,
)
from vlytics.engine.market import (
    EVALUATOR_VERSION as MARKET_EVALUATOR_VERSION,
    EvaluationEligibility,
    MarketEvaluation,
)


def identifier(run_id: str, name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://vlytics.test/live-e2e/{run_id}/{name}")


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def insert(connection: Any, statement: str, values: dict[str, object]) -> None:
    connection.execute(text(statement), values)


def seed(database_url: str, run_id: str) -> dict[str, object]:
    ids = {
        name: identifier(run_id, name)
        for name in (
            "raw",
            "season",
            "competition",
            "home",
            "away",
            "match",
            "schedule",
            "feature",
            "variant-openai",
            "variant-statistical",
            "prediction-openai",
            "prediction-statistical",
            "event-openai",
            "event-statistical",
            "result",
            "evaluation-openai",
            "evaluation-statistical",
            "coverage",
            "job",
        )
    }
    now = datetime.now(UTC).replace(microsecond=0)
    kst_day = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    scheduled = datetime.combine(kst_day, time(19, 30), ZoneInfo("Asia/Seoul")).astimezone(UTC)
    cutoff = scheduled - timedelta(hours=1)
    source = f"live-e2e-{run_id}"
    competition = f"live-e2e-{run_id}"
    home_team = f"Live Home Club {run_id}"
    away_team = f"Live Away Club {run_id}"
    output_openai = {
        "schema_version": "prediction-v1",
        "capabilities": ["winner", "set_score"],
        "home_win_probability": 0.64,
        "set_score_probabilities": [
            {"outcome": "3:0", "probability": 0.18},
            {"outcome": "3:1", "probability": 0.24},
            {"outcome": "3:2", "probability": 0.22},
            {"outcome": "2:3", "probability": 0.15},
            {"outcome": "1:3", "probability": 0.12},
            {"outcome": "0:3", "probability": 0.09},
        ],
        "rationale": "Live E2E synthetic database fact.",
        "risk_factors": ["Synthetic smoke data only."],
    }
    output_statistical = {
        "schema_version": "prediction-v1",
        "capabilities": ["winner"],
        "home_win_probability": 0.58,
        "rationale": "Live E2E statistical baseline.",
        "risk_factors": [],
    }
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            insert(
                connection,
                """
                INSERT INTO mirror.raw_snapshots (
                    id, source, source_group_code, request_fingerprint, redacted_url,
                    requested_at, received_at, status_code, parser_version
                ) VALUES (:id, :source, 'live-e2e', :fingerprint, 'synthetic://live-e2e',
                          :now, :now, 200, 'live-e2e-v1') ON CONFLICT DO NOTHING
            """,
                {"id": ids["raw"], "source": source, "fingerprint": source, "now": now},
            )
            insert(
                connection,
                """
                INSERT INTO mirror.seasons (
                    id, source, source_group_code, source_season_code, label,
                    starts_at, ends_at, observed_at, raw_snapshot_id
                ) VALUES (:id, :source, 'live-e2e', :code, :code, :start_at,
                          :end_at, :now, :raw_id) ON CONFLICT DO NOTHING
            """,
                {
                    "id": ids["season"],
                    "source": source,
                    "code": source,
                    "start_at": scheduled - timedelta(days=30),
                    "end_at": scheduled + timedelta(days=30),
                    "now": now,
                    "raw_id": ids["raw"],
                },
            )
            insert(
                connection,
                """
                INSERT INTO mirror.competitions (
                    id, source, source_group_code, season_id, source_competition_code,
                    division, stage, label, mapping_version, observed_at, raw_snapshot_id
                ) VALUES (:id, :source, 'live-e2e', :season_id, :competition,
                          'women', 'regular', :competition, 'live-e2e-v1', :now, :raw_id)
                ON CONFLICT DO NOTHING
            """,
                {
                    "id": ids["competition"],
                    "source": source,
                    "season_id": ids["season"],
                    "competition": competition,
                    "now": now,
                    "raw_id": ids["raw"],
                },
            )
            for key, code, name in (
                ("home", "LIVE-HOME", home_team),
                ("away", "LIVE-AWAY", away_team),
            ):
                insert(
                    connection,
                    """
                    INSERT INTO mirror.team_identities (
                        id, source, source_team_code, season_id, display_name,
                        observed_at, mapping_version, raw_snapshot_id
                    ) VALUES (:id, :source, :code, :season_id, :name, :now,
                              'live-e2e-v1', :raw_id) ON CONFLICT DO NOTHING
                """,
                    {
                        "id": ids[key],
                        "source": source,
                        "code": f"{code}-{run_id}",
                        "season_id": ids["season"],
                        "name": name,
                        "now": now,
                        "raw_id": ids["raw"],
                    },
                )
            insert(
                connection,
                """
                INSERT INTO mirror.matches (
                    id, source, source_group_code, source_season_code,
                    source_competition_code, source_match_code, season_id,
                    competition_id, first_observed_at, raw_snapshot_id
                ) VALUES (:id, :source, 'live-e2e', :source, :competition,
                          :match_code, :season_id, :competition_id, :now, :raw_id)
                ON CONFLICT DO NOTHING
            """,
                {
                    "id": ids["match"],
                    "source": source,
                    "competition": competition,
                    "match_code": f"match-{run_id}",
                    "season_id": ids["season"],
                    "competition_id": ids["competition"],
                    "now": now,
                    "raw_id": ids["raw"],
                },
            )
            insert(
                connection,
                """
                INSERT INTO mirror.match_revisions (
                    id, match_id, revision, home_team_id, away_team_id,
                    scheduled_start_at, actual_start_at, status, observed_at, raw_snapshot_id
                ) VALUES (:id, :match_id, 1, :home_id, :away_id, :scheduled,
                          :scheduled, 'finished', :now, :raw_id) ON CONFLICT DO NOTHING
            """,
                {
                    "id": ids["schedule"],
                    "match_id": ids["match"],
                    "home_id": ids["home"],
                    "away_id": ids["away"],
                    "scheduled": scheduled,
                    "now": now,
                    "raw_id": ids["raw"],
                },
            )
            insert(
                connection,
                """
                INSERT INTO engine.feature_snapshots (
                    id, match_id, schedule_revision_id, cutoff_at, captured_at,
                    feature_version, availability_policy, lineup_status,
                    values_json, lineage_json, sha256
                ) VALUES (:id, :match_id, :schedule_id, :cutoff, :cutoff,
                          'live-feature-v1', 'live_prospective', 'unknown',
                          '{}'::jsonb, '{}'::jsonb, :sha) ON CONFLICT DO NOTHING
            """,
                {
                    "id": ids["feature"],
                    "match_id": ids["match"],
                    "schedule_id": ids["schedule"],
                    "cutoff": cutoff,
                    "sha": digest({"run_id": run_id, "kind": "feature"}),
                },
            )
            for provider, model in (
                ("openai", "live-openai-model"),
                ("statistical", "live-stat-model"),
            ):
                key = f"variant-{provider}"
                insert(
                    connection,
                    """
                    INSERT INTO engine.model_variants (
                        id, provider, requested_model, pinned_model_version,
                        prompt_version, prompt_hash, feature_version,
                        output_schema_version, hyperparameters, experiment_group
                    ) VALUES (:id, :provider, :model, :model, :prompt, :hash,
                              'live-feature-v1', 'prediction-v1', '{}'::jsonb, :group_name)
                    ON CONFLICT DO NOTHING
                """,
                    {
                        "id": ids[key],
                        "provider": provider,
                        "model": model,
                        "prompt": "live-prompt-v1" if provider == "openai" else "not-applicable",
                        "hash": digest({"run_id": run_id, "provider": provider}),
                        "group_name": f"live-e2e-{run_id}-{provider}",
                    },
                )
            for provider, output in (
                ("openai", output_openai),
                ("statistical", output_statistical),
            ):
                prediction_key = f"prediction-{provider}"
                event_key = f"event-{provider}"
                insert(
                    connection,
                    """
                    INSERT INTO engine.predictions (
                        id, match_id, schedule_revision_id, snapshot_id, variant_id,
                        stage, input_cutoff_at, started_at, generated_at,
                        resolved_model_id, output_json, sha256
                    ) VALUES (:id, :match_id, :schedule_id, :snapshot_id, :variant_id,
                              :stage, :cutoff, :cutoff, :generated, :model,
                              CAST(:output AS jsonb), :sha) ON CONFLICT DO NOTHING
                """,
                    {
                        "id": ids[prediction_key],
                        "match_id": ids["match"],
                        "schedule_id": ids["schedule"],
                        "snapshot_id": ids["feature"],
                        "variant_id": ids[f"variant-{provider}"],
                        "stage": "winner" if provider == "openai" else "statistical",
                        "cutoff": cutoff,
                        "generated": cutoff + timedelta(minutes=1),
                        "model": "live-openai-model" if provider == "openai" else "live-stat-model",
                        "output": json.dumps(output),
                        "sha": digest(output),
                    },
                )
                insert(
                    connection,
                    """
                    INSERT INTO engine.prediction_events (
                        id, prediction_id, event_type, reason, occurred_at,
                        observed_at, schedule_revision_id
                    ) VALUES (:id, :prediction_id, 'published', 'live_e2e_seed',
                              :occurred_at, :occurred_at, :schedule_id)
                    ON CONFLICT DO NOTHING
                """,
                    {
                        "id": ids[event_key],
                        "prediction_id": ids[prediction_key],
                        "occurred_at": cutoff + timedelta(minutes=1),
                        "schedule_id": ids["schedule"],
                    },
                )
                insert(
                    connection,
                    """
                    INSERT INTO engine.prediction_status_projection (
                        prediction_id, latest_event_id, current_status
                    ) VALUES (:prediction_id, :event_id, 'published')
                    ON CONFLICT DO NOTHING
                """,
                    {"prediction_id": ids[prediction_key], "event_id": ids[event_key]},
                )
            insert(
                connection,
                """
                INSERT INTO mirror.result_revisions (
                    id, match_id, revision, home_sets, away_sets, home_points,
                    away_points, finality, observed_at, raw_snapshot_id, rule_version
                ) VALUES (:id, :match_id, 1, 3, 1, 96, 84, 'final', :now,
                          :raw_id, 'live-e2e-v1')
                ON CONFLICT DO NOTHING
            """,
                {"id": ids["result"], "match_id": ids["match"], "now": now, "raw_id": ids["raw"]},
            )
            result = ResultRevision(
                result_revision_id=str(ids["result"]),
                match_id=str(ids["match"]),
                revision=1,
                finality="final",
                home_sets=3,
                away_sets=1,
                home_points=96,
                away_points=84,
            )
            for provider, output in (
                ("openai", output_openai),
                ("statistical", output_statistical),
            ):
                model_version = (
                    "live-openai-model" if provider == "openai" else "live-stat-model"
                )
                prompt_version = (
                    "live-prompt-v1" if provider == "openai" else "not-applicable"
                )
                prediction = PredictionEvaluationInput(
                    prediction_id=str(ids[f"prediction-{provider}"]),
                    match_id=str(ids["match"]),
                    schedule_revision_id=str(ids["schedule"]),
                    snapshot_id=str(ids["feature"]),
                    input_cutoff_at=cutoff,
                    cohort=CohortKey(
                        division="women",
                        competition=competition,
                        stage="regular",
                        provider=provider,
                        model_version=model_version,
                        prompt_version=prompt_version,
                        feature_version="live-feature-v1",
                        availability_policy="live_prospective",
                        timing_eligibility="on_time",
                        result_finality="final",
                    ),
                    home_win_probability=float(output["home_win_probability"]),
                    set_score_probabilities=(
                        {
                            str(item["outcome"]): float(item["probability"])
                            for item in output.get("set_score_probabilities", [])
                        }
                        or None
                    ),
                )
                evaluation = ResultEvaluator().evaluate(
                    prediction,
                    result,
                    market_evaluation=MarketEvaluation(
                        prediction_id=prediction.prediction_id,
                        match_id=prediction.match_id,
                        snapshot_id=None,
                        evaluator_version=MARKET_EVALUATOR_VERSION,
                        eligibility=EvaluationEligibility.MISSING,
                        reason="no_market_evaluation_for_prediction",
                        lines=(),
                    ),
                )
                insert(
                    connection,
                    """
                    INSERT INTO engine.evaluations (
                        id, match_id, prediction_id, result_revision_id,
                        evaluator_version, cohort_policy_version, metric_values, settlement
                    ) VALUES (:id, :match_id, :prediction_id, :result_id,
                              :evaluator_version, :cohort_policy_version,
                              CAST(:metrics AS jsonb),
                              CAST(:settlement AS jsonb))
                    ON CONFLICT DO NOTHING
                """,
                    {
                        "id": ids[f"evaluation-{provider}"],
                        "match_id": ids["match"],
                        "prediction_id": ids[f"prediction-{provider}"],
                        "result_id": ids["result"],
                        "evaluator_version": EVALUATOR_VERSION,
                        "cohort_policy_version": COHORT_POLICY_VERSION,
                        "metrics": json.dumps(evaluation.metric_values()),
                        "settlement": json.dumps(evaluation.settlement_values()),
                    },
                )
            insert(
                connection,
                """
                INSERT INTO mirror.source_coverage (
                    id, source, season_id, competition_id, match_id, data_kind,
                    availability, evidence, observed_at, raw_snapshot_id
                ) VALUES (:id, :source, :season_id, :competition_id, :match_id,
                          'schedule', 'available', 'LIVE-E2E seeded', :now, :raw_id)
                ON CONFLICT DO NOTHING
            """,
                {
                    "id": ids["coverage"],
                    "source": source,
                    "season_id": ids["season"],
                    "competition_id": ids["competition"],
                    "match_id": ids["match"],
                    "now": now,
                    "raw_id": ids["raw"],
                },
            )
            insert(
                connection,
                """
                INSERT INTO ops.jobs (
                    id, job_key, job_type, payload, due_at, deadline_at,
                    state, attempt_no, error_code
                ) VALUES (:id, :job_key, 'live-e2e.retry', '{}'::jsonb, :due_at,
                          :deadline_at, 'failed', 1, 'live_e2e_failure')
                ON CONFLICT DO NOTHING
            """,
                {
                    "id": ids["job"],
                    "job_key": f"live-e2e-retry:{run_id}",
                    "due_at": now,
                    "deadline_at": now + timedelta(days=1),
                },
            )
    finally:
        engine.dispose()
    return {
        "run_id": run_id,
        "schedule_date": kst_day.isoformat(),
        "match_id": str(ids["match"]),
        "job_id": str(ids["job"]),
        "competition": competition,
        "home_team": home_team,
        "away_team": away_team,
        "providers": ["openai", "statistical"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True, help="Unique non-secret CI run identifier")
    parser.add_argument("--database-url", default=os.getenv("VLYTICS_LIVE_E2E_DATABASE_URL"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or VLYTICS_LIVE_E2E_DATABASE_URL is required")
    manifest = seed(args.database_url, args.run_id)
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
