from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from runpy import run_path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from vlytics.api.app import create_app
from vlytics.api.repository import (
    InMemoryReadRepository,
    PostgresReadRepository,
    ReadQuery,
    ReadSnapshot,
)

OPERATOR_HEADERS = {"Authorization": "Bearer synthetic-operator-secret"}
JOB_ID = "11111111-1111-1111-1111-111111111111"


def _seed_postgres_graph(postgres_role_urls: object, run_id: str) -> dict[str, object]:
    script = Path(__file__).resolve().parents[3] / "infra" / "scripts" / "seed_browser_smoke.py"
    seed_function = run_path(str(script))["seed"]
    return seed_function(str(postgres_role_urls.migrator), run_id)


def _postgres_client(postgres_role_urls: object) -> TestClient:
    repository = PostgresReadRepository(create_engine(str(postgres_role_urls.read_api)))
    return TestClient(
        create_app(
            repository=repository,
            environ={"VLYTICS_OPERATOR_AUTH_SECRET": "synthetic-operator-secret"},
        )
    )


def _match(
    identifier: str,
    scheduled_at: datetime,
    *,
    market: bool = True,
) -> dict[str, object]:
    return {
        "id": identifier,
        "competition": "regular-2026",
        "division": "women",
        "stage": "regular",
        "schedule_revision_id": f"schedule-{identifier}",
        "source_snapshot_id": f"source-{identifier}",
        "scheduled_start_at": scheduled_at,
        "actual_start_at": None,
        "status": "scheduled",
        "home_team_id": "team-home",
        "home_team_code": "HOME",
        "home_team_name": "Home Club",
        "away_team_id": "team-away",
        "away_team_code": "AWAY",
        "away_team_name": "Away Club",
        "venue": "Synthetic Arena",
        "result": None,
        "market_snapshot_id": f"market-{identifier}" if market else None,
        "market_source": "synthetic" if market else None,
        "market_quoted_at": scheduled_at if market else None,
    }


def _prediction(
    identifier: str,
    match_id: str,
    generated_at: datetime,
    *,
    provider: str = "openai",
    status: str = "succeeded",
    lifecycle_status: str | None = "published",
    schedule_revision_id: str | None = None,
    feature_snapshot_id: str | None = None,
    input_cutoff_at: datetime | None = None,
    market_eligibility: str | None = "eligible",
    market_snapshot_id: str | None = None,
    requested_model: str | None = None,
    variant_id: str | None = None,
) -> dict[str, object]:
    has_prediction = status == "succeeded"
    return {
        "id": identifier,
        "record_type": "prediction" if has_prediction else "attempt",
        "prediction_revision_id": identifier if has_prediction else None,
        "attempt_id": None if has_prediction else identifier,
        "match_id": match_id,
        "competition": "regular-2026",
        "division": "women",
        "competition_stage": "regular",
        "provider": provider,
        "variant_id": variant_id or f"variant-{provider}",
        "prediction_type": "winner",
        "requested_model": requested_model or f"{provider}-synthetic",
        "resolved_model_id": f"{provider}-synthetic-2026-09" if has_prediction else None,
        "model_version": "2026-09",
        "prompt_version": "prompt-v1",
        "feature_version": "feature-v2",
        "availability_policy": "live_prospective",
        "schedule_revision_id": schedule_revision_id or f"schedule-{match_id}",
        "source_snapshot_id": f"source-{match_id}",
        "home_team_id": "team-home",
        "home_team_code": "HOME",
        "away_team_id": "team-away",
        "away_team_code": "AWAY",
        "feature_snapshot_id": feature_snapshot_id or f"feature-{match_id}",
        "input_cutoff_at": input_cutoff_at or generated_at,
        "generated_at": generated_at,
        "status": lifecycle_status if has_prediction else status,
        "lifecycle_status": lifecycle_status if has_prediction else None,
        "provider_status": status,
        "error_code": "provider_timeout" if status != "succeeded" else None,
        "market_eligibility": market_eligibility if has_prediction else None,
        "market_reason": None if has_prediction else "prediction_market_provenance_unavailable",
        "market_snapshot_id": (
            market_snapshot_id or f"market-{match_id}" if has_prediction else None
        ),
        "market_source": "synthetic" if has_prediction else None,
        "market_quoted_at": generated_at if has_prediction else None,
        "output": {"home_win_probability": 0.6} if status == "succeeded" else {},
    }


def _client(*, now: datetime | None = None) -> TestClient:
    matches = (
        _match("match-1", datetime(2026, 9, 20, 14, 30, tzinfo=UTC)),
        _match("match-2", datetime(2026, 9, 20, 15, 30, tzinfo=UTC), market=False),
        _match("match-3", datetime(2026, 9, 20, 16, 30, tzinfo=UTC)),
    )
    predictions = (
        _prediction("prediction-1", "match-1", datetime(2026, 9, 20, 13, 30, tzinfo=UTC)),
        _prediction(
            "prediction-2",
            "match-2",
            datetime(2026, 9, 20, 14, 30, tzinfo=UTC),
            provider="anthropic",
            status="timed_out",
        ),
        _prediction("prediction-3", "match-3", datetime(2026, 9, 20, 15, 30, tzinfo=UTC)),
        {
            **_prediction(
                "prediction-stat-1",
                "match-1",
                datetime(2026, 9, 20, 13, 30, tzinfo=UTC),
                provider="statistical",
            ),
            "prediction_type": "statistical",
            "model_version": "elo-v1",
            "prompt_version": "not-applicable",
        },
    )
    evaluations = (
        {
            "id": "evaluation-1",
            "prediction_id": "prediction-1",
            "result_revision_id": "result-1",
            "result_revision_number": 1,
            "result_finality": "final",
            "evaluator_version": "eval-v1",
            "cohort_policy_version": "cohort-v1",
            "metric_values": {
                "eligible": True,
                "home_win_probability": 0.6,
                "home_win_outcome": 1,
                "brier": 0.16,
                "log_loss": 0.5108256238,
                "winner_accuracy": 1.0,
                "set_rps": 0.12,
                "set_score_accuracy": 1.0,
                "cohort": {
                    "division": "women",
                    "competition": "regular-2026",
                    "stage": "regular",
                    "provider": "openai",
                    "model_version": "2026-09",
                    "prompt_version": "prompt-v1",
                    "feature_version": "feature-v2",
                    "availability_policy": "live_prospective",
                    "timing_eligibility": "on_time",
                    "result_finality": "final",
                },
            },
            "settlement": {"market_status": "eligible", "market": []},
            "match_id": "match-1",
            "prediction_type": "winner",
            "provider": "openai",
            "model_version": "2026-09",
            "prompt_version": "prompt-v1",
            "feature_version": "feature-v2",
            "availability_policy": "live_prospective",
            "division": "women",
            "competition": "regular-2026",
            "stage": "regular",
            "schedule_revision_id": "schedule-match-1",
            "feature_snapshot_id": "feature-match-1",
            "input_cutoff_at": datetime(2026, 9, 20, 13, 30, tzinfo=UTC),
            "match_start_at": datetime(2026, 9, 20, 14, 30, tzinfo=UTC),
            "evaluation_created_at": datetime(2026, 9, 20, 17, 0, tzinfo=UTC),
            "market_availability": "eligible",
            "market_home_probability": 0.55,
        },
        {
            "id": "evaluation-stat-1",
            "prediction_id": "prediction-stat-1",
            "result_revision_id": "result-1",
            "result_revision_number": 1,
            "result_finality": "final",
            "evaluator_version": "eval-v1",
            "cohort_policy_version": "cohort-v1",
            "metric_values": {
                "eligible": True,
                "home_win_probability": 0.5,
                "home_win_outcome": 1,
                "brier": 0.25,
                "log_loss": 0.6931471806,
                "winner_accuracy": 1.0,
                "set_rps": 0.2,
                "set_score_accuracy": 0.0,
                "cohort": {
                    "division": "women",
                    "competition": "regular-2026",
                    "stage": "regular",
                    "provider": "statistical",
                    "model_version": "elo-v1",
                    "prompt_version": "not-applicable",
                    "feature_version": "feature-v2",
                    "availability_policy": "live_prospective",
                    "timing_eligibility": "on_time",
                    "result_finality": "final",
                },
            },
            "settlement": {"market_status": "eligible", "market": []},
            "match_id": "match-1",
            "prediction_type": "statistical",
            "provider": "statistical",
            "model_version": "elo-v1",
            "prompt_version": "not-applicable",
            "feature_version": "feature-v2",
            "availability_policy": "live_prospective",
            "division": "women",
            "competition": "regular-2026",
            "stage": "regular",
            "schedule_revision_id": "schedule-match-1",
            "feature_snapshot_id": "feature-match-1",
            "input_cutoff_at": datetime(2026, 9, 20, 13, 30, tzinfo=UTC),
            "match_start_at": datetime(2026, 9, 20, 14, 30, tzinfo=UTC),
            "evaluation_created_at": datetime(2026, 9, 20, 17, 0, tzinfo=UTC),
            "market_availability": "eligible",
            "market_home_probability": 0.55,
        },
    )
    operations = (
        {
            "id": JOB_ID,
            "job_type": "engine.run_prediction",
            "state": "failed",
            "due_at": datetime(2026, 9, 20, 13, 25, tzinfo=UTC),
            "deadline_at": datetime(2026, 9, 20, 13, 35, tzinfo=UTC),
            "attempt_no": 1,
            "error_code": "provider_timeout",
            "schedule_revision_id": "schedule-match-1",
        },
    )
    coverage = (
        {
            "id": "coverage-1",
            "data_kind": "market",
            "availability": "missing",
            "observed_at": datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
            "evidence_code": "OP-005",
            "source_snapshot_id": "source-match-2",
            "match_id": "match-2",
        },
    )
    budgets = (
        {
            "provider": "openai",
            "currency": "USD",
            "period": "day",
            "period_start": datetime(2026, 9, 20, tzinfo=UTC).date(),
            "period_end": datetime(2026, 9, 21, tzinfo=UTC).date(),
            "as_of": datetime(2026, 9, 20, 13, 31, tzinfo=UTC),
            "reserved_amount": "1.50000000",
            "actual_amount": "0.70000000",
            "effective_amount": "1.20000000",
            "reservation_count": 2,
            "settled_count": 1,
            "outstanding_count": 1,
            "conservative_charge_count": 0,
        },
        {
            "provider": "openai",
            "currency": "USD",
            "period": "month",
            "period_start": datetime(2026, 9, 1, tzinfo=UTC).date(),
            "period_end": datetime(2026, 10, 1, tzinfo=UTC).date(),
            "as_of": datetime(2026, 9, 20, 13, 31, tzinfo=UTC),
            "reserved_amount": "3.00000000",
            "actual_amount": "2.20000000",
            "effective_amount": "2.70000000",
            "reservation_count": 4,
            "settled_count": 3,
            "outstanding_count": 1,
            "conservative_charge_count": 1,
        },
    )
    repository = InMemoryReadRepository(
        ReadSnapshot(matches, predictions, evaluations, operations, coverage, "revision-1", budgets)
    )
    return TestClient(
        create_app(
            repository=repository,
            environ={
                "VLYTICS_OPERATOR_AUTH_SECRET": "synthetic-operator-secret",
                "VLYTICS_READONLY_AUTH_SECRET": "synthetic-readonly-secret",
            },
            clock=lambda: now or datetime(2026, 9, 20, 13, 30, tzinfo=UTC),
        )
    )


def test_operator_routes_require_authentication_and_role() -> None:
    client = _client()

    assert client.get("/api/v1/schedule?date=2026-09-20").status_code == 401
    assert (
        client.get(
            "/api/v1/schedule?date=2026-09-20",
            headers={"Authorization": "Bearer synthetic-readonly-secret"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/schedule?date=2026-09-20",
            headers={"Authorization": "Bearer invalid"},
        ).status_code
        == 401
    )


def test_invalid_filters_and_cursor_are_rejected() -> None:
    client = _client()

    assert (
        client.get(
            "/api/v1/schedule?date=2026-09-20&division=unknown",
            headers=OPERATOR_HEADERS,
        ).status_code
        == 422
    )
    timezone_error = client.get(
        "/api/v1/predictions?start_at=2026-09-20T00:00:00",
        headers=OPERATOR_HEADERS,
    ).json()
    assert timezone_error["code"] == "timezone_required"
    assert set(timezone_error) == {"code", "message", "retryable", "correlation_id"}
    performance_timezone_error = client.get(
        "/api/v1/performance?start_at=2026-09-20T00:00:00",
        headers=OPERATOR_HEADERS,
    ).json()
    assert performance_timezone_error["code"] == "timezone_required"
    performance_range_error = client.get(
        "/api/v1/performance?start_at=2026-09-21T00:00:00Z&end_at=2026-09-20T00:00:00Z",
        headers=OPERATOR_HEADERS,
    ).json()
    assert performance_range_error["code"] == "invalid_time_range"
    cursor_error = client.get(
        "/api/v1/schedule?date=2026-09-20&cursor=not-a-cursor",
        headers=OPERATOR_HEADERS,
    ).json()
    assert cursor_error["code"] == "invalid_cursor"


def test_cursor_boundaries_have_no_duplicates_or_omissions() -> None:
    client = _client()
    cursor: str | None = None
    identifiers: list[str] = []

    while True:
        params = {"date": "2026-09-20", "timezone": "UTC", "limit": "1"}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/api/v1/schedule", params=params, headers=OPERATOR_HEADERS)
        assert response.status_code == 200
        payload = response.json()["data"]
        identifiers.extend(item["id"] for item in payload["items"])
        cursor = payload["next_cursor"]
        if cursor is None:
            break

    assert identifiers == ["match-1", "match-2", "match-3"]
    assert len(identifiers) == len(set(identifiers))

    first = client.get(
        "/api/v1/schedule?date=2026-09-20&timezone=UTC&limit=1",
        headers=OPERATOR_HEADERS,
    ).json()
    mismatch = client.get(
        "/api/v1/schedule",
        params={
            "date": "2026-09-20",
            "timezone": "UTC",
            "division": "women",
            "limit": 1,
            "cursor": first["data"]["next_cursor"],
        },
        headers=OPERATOR_HEADERS,
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "cursor_filter_mismatch"


def test_schedule_applies_utc_and_kst_calendar_boundaries() -> None:
    client = _client()

    utc = client.get(
        "/api/v1/schedule?date=2026-09-20&timezone=UTC",
        headers=OPERATOR_HEADERS,
    ).json()
    kst = client.get(
        "/api/v1/schedule?date=2026-09-21&timezone=Asia%2FSeoul",
        headers=OPERATOR_HEADERS,
    ).json()

    assert [item["id"] for item in utc["data"]["items"]] == [
        "match-1",
        "match-2",
        "match-3",
    ]
    assert [item["id"] for item in kst["data"]["items"]] == ["match-2", "match-3"]
    assert kst["data"]["items"][0]["scheduled_start_at_local"].startswith(
        "2026-09-21T00:30:00+09:00"
    )


def test_empty_partial_provider_failure_and_market_missing_are_explicit() -> None:
    client = _client()

    empty = client.get("/api/v1/schedule?date=2030-01-01", headers=OPERATOR_HEADERS).json()
    detail = client.get("/api/v1/matches/match-2", headers=OPERATOR_HEADERS).json()

    assert empty["data"] == {
        "date": "2030-01-01",
        "timezone": "Asia/Seoul",
        "items": [],
        "next_cursor": None,
    }
    assert empty["metadata"]["schema_version"] == "vlytics.operator.v1"
    assert detail["data"]["market"] == {
        "availability": "missing",
        "source": None,
        "snapshot_id": None,
        "quoted_at": None,
        "reason": "prediction_market_provenance_unavailable",
    }
    assert detail["data"]["provider_outcomes"][0]["status"] == "timed_out"
    assert detail["data"]["provider_outcomes"][0]["error_code"] == "provider_timeout"
    assert "raw" not in str(detail).lower()


def test_frontend_contract_fixture_matches_actual_api_serialization() -> None:
    client = _client()
    fixture_path = (
        Path(__file__).parents[3]
        / "frontend"
        / "tests"
        / "fixtures"
        / "operator-api.serializer.json"
    )
    fixture = __import__("json").loads(fixture_path.read_text(encoding="utf-8"))

    assert fixture == {
        "schedule": client.get(
            "/api/v1/schedule?date=2026-09-20&timezone=UTC",
            headers=OPERATOR_HEADERS,
        ).json(),
        "match": client.get(
            "/api/v1/matches/match-1?timezone=Asia%2FSeoul",
            headers=OPERATOR_HEADERS,
        ).json(),
        "history": client.get(
            "/api/v1/predictions?provider=openai&limit=1",
            headers=OPERATOR_HEADERS,
        ).json(),
    }


def test_failed_attempt_serialization_keeps_prediction_provenance_empty() -> None:
    client = _client()

    schedule = client.get(
        "/api/v1/schedule?date=2026-09-20&timezone=UTC",
        headers=OPERATOR_HEADERS,
    ).json()
    failed_match = next(item for item in schedule["data"]["items"] if item["id"] == "match-2")
    failed_outcome = failed_match["provider_outcomes"][0]
    assert failed_outcome["status"] == "timed_out"
    assert failed_outcome["prediction_revision_id"] is None
    assert failed_outcome["attempt_id"] == "prediction-2"
    assert failed_outcome["requested_model"] == "anthropic-synthetic"
    assert failed_outcome["resolved_model_id"] is None
    assert "prediction-2" not in schedule["metadata"]["prediction_revision_ids"]

    history = client.get(
        "/api/v1/predictions?provider=anthropic",
        headers=OPERATOR_HEADERS,
    ).json()
    assert history["data"]["items"] == [
        {
            "id": "prediction-2",
            "record_type": "attempt",
            "prediction_revision_id": None,
            "attempt_id": "prediction-2",
            "match_id": "match-2",
            "competition": "regular-2026",
            "division": "women",
            "provider": "anthropic",
            "variant_id": "variant-anthropic",
            "prediction_type": "winner",
            "requested_model": "anthropic-synthetic",
            "resolved_model_id": None,
            "model_version": "2026-09",
            "prompt_version": "prompt-v1",
            "feature_version": "feature-v2",
            "schedule_revision_id": "schedule-match-2",
            "source_snapshot_id": "source-match-2",
            "generated_at": "2026-09-20T14:30:00Z",
            "status": "timed_out",
            "output": {},
            "evaluation_revision_id": None,
        }
    ]
    assert history["metadata"]["prediction_revision_ids"] == []
    assert history["metadata"]["model_versions"] == {}


def test_match_detail_selects_current_shared_snapshot_and_published_lifecycle() -> None:
    client = _client()
    repository = client.app.state.read_repository
    generated = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
    current = (
        _prediction(
            "current-openai",
            "match-1",
            generated,
            provider="openai",
            feature_snapshot_id="feature-shared",
            input_cutoff_at=generated,
        ),
        _prediction(
            "current-statistical",
            "match-1",
            generated,
            provider="statistical",
            feature_snapshot_id="feature-shared",
            input_cutoff_at=generated,
        ),
        _prediction(
            "newer-partial",
            "match-1",
            datetime(2026, 9, 20, 13, 20, tzinfo=UTC),
            provider="anthropic",
            feature_snapshot_id="feature-partial",
        ),
        _prediction(
            "voided-current",
            "match-1",
            generated,
            provider="google",
            lifecycle_status="voided",
            feature_snapshot_id="feature-shared",
            input_cutoff_at=generated,
        ),
        _prediction(
            "old-schedule",
            "match-1",
            generated,
            provider="google",
            schedule_revision_id="schedule-match-1-old",
            feature_snapshot_id="feature-shared",
            input_cutoff_at=generated,
        ),
        _prediction(
            "legacy-no-lifecycle",
            "match-1",
            generated,
            provider="anthropic",
            lifecycle_status=None,
            feature_snapshot_id="feature-shared",
            input_cutoff_at=generated,
        ),
    )
    repository.snapshot = ReadSnapshot(
        repository.snapshot.matches,
        current,
        repository.snapshot.evaluations,
        repository.snapshot.operations,
        repository.snapshot.coverage,
        "revision-current-predictions",
        repository.snapshot.budgets,
    )

    response = client.get("/api/v1/matches/match-1", headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    detail = response.json()["data"]
    assert detail["feature_snapshot_id"] == "feature-shared"
    assert [row["prediction_revision_id"] for row in detail["provider_outcomes"]] == [
        "current-openai",
        "current-statistical",
    ]
    assert {row["lifecycle_status"] for row in detail["provider_outcomes"]} == {"published"}
    assert {row["schedule_revision_id"] for row in detail["provider_outcomes"]} == {
        "schedule-match-1"
    }
    assert {row["input_cutoff_at"] for row in detail["provider_outcomes"]} == {
        "2026-09-20T13:00:00Z"
    }


def test_schedule_model_filter_selects_matching_variant_before_provider_representative() -> None:
    client = _client()
    repository = client.app.state.read_repository
    cutoff = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
    repository.snapshot = ReadSnapshot(
        repository.snapshot.matches,
        (
            _prediction(
                "target-variant-prediction",
                "match-1",
                cutoff + timedelta(minutes=1),
                provider="openai",
                feature_snapshot_id="feature-shared",
                input_cutoff_at=cutoff,
                requested_model="openai-target",
                variant_id="variant-openai-target",
            ),
            _prediction(
                "newer-other-variant",
                "match-1",
                cutoff + timedelta(minutes=2),
                provider="openai",
                feature_snapshot_id="feature-shared",
                input_cutoff_at=cutoff,
                requested_model="openai-other",
                variant_id="variant-openai-other",
            ),
        ),
        repository.snapshot.evaluations,
        repository.snapshot.operations,
        repository.snapshot.coverage,
        "revision-variant-filter",
        repository.snapshot.budgets,
    )

    response = client.get(
        "/api/v1/schedule?date=2026-09-20&timezone=UTC&model=openai-target",
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 200
    outcomes = response.json()["data"]["items"][0]["provider_outcomes"]
    assert [(row["prediction_revision_id"], row["variant_id"]) for row in outcomes] == [
        ("target-variant-prediction", "variant-openai-target")
    ]


def test_provider_market_summary_preserves_as_of_eligibility() -> None:
    client = _client()
    repository = client.app.state.read_repository
    cutoff = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
    predictions = (
        _prediction(
            "market-eligible",
            "match-1",
            cutoff,
            provider="openai",
            feature_snapshot_id="feature-market",
            input_cutoff_at=cutoff,
            market_eligibility="eligible",
            market_snapshot_id="quote-eligible",
        ),
        {
            **_prediction(
                "market-stale",
                "match-1",
                cutoff,
                provider="anthropic",
                feature_snapshot_id="feature-market",
                input_cutoff_at=cutoff,
                market_eligibility="stale",
                market_snapshot_id="quote-stale",
            ),
            "market_reason": "quote_age_exceeded",
        },
        {
            **_prediction(
                "market-late",
                "match-1",
                cutoff,
                provider="statistical",
                feature_snapshot_id="feature-market",
                input_cutoff_at=cutoff,
                market_eligibility="late",
                market_snapshot_id="quote-late",
            ),
            "market_reason": "received_after_input_cutoff",
        },
    )
    repository.snapshot = ReadSnapshot(
        repository.snapshot.matches,
        predictions,
        repository.snapshot.evaluations,
        repository.snapshot.operations,
        repository.snapshot.coverage,
        "revision-market-as-of",
        repository.snapshot.budgets,
    )

    detail = client.get("/api/v1/matches/match-1", headers=OPERATOR_HEADERS).json()["data"]
    by_provider = {row["provider"]: row["market"] for row in detail["provider_outcomes"]}
    assert by_provider["openai"]["availability"] == "available"
    assert by_provider["anthropic"] == {
        "availability": "stale",
        "source": "synthetic",
        "snapshot_id": "quote-stale",
        "quoted_at": "2026-09-20T13:00:00Z",
        "reason": "quote_age_exceeded",
    }
    assert by_provider["statistical"]["availability"] == "late"
    assert detail["market"] == {
        "availability": "unverified",
        "source": None,
        "snapshot_id": None,
        "quoted_at": None,
        "reason": "provider_market_summaries_differ",
    }


def test_coverage_uses_latest_observation_per_source_scope() -> None:
    client = _client()
    repository = client.app.state.read_repository
    base = {
        "source": "synthetic",
        "season_id": "season-1",
        "competition_id": "competition-1",
        "match_id": "match-2",
        "data_kind": "lineup",
        "evidence_code": "OP-005",
        "source_snapshot_id": "source-match-2",
    }
    coverage = (
        {
            **base,
            "id": "coverage-old",
            "availability": "missing",
            "observed_at": datetime(2026, 9, 20, 13, 0, tzinfo=UTC),
        },
        {
            **base,
            "id": "coverage-y",
            "availability": "missing",
            "observed_at": datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        },
        {
            **base,
            "id": "coverage-z",
            "availability": "available",
            "observed_at": datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        },
    )
    repository.snapshot = ReadSnapshot(
        repository.snapshot.matches,
        repository.snapshot.predictions,
        repository.snapshot.evaluations,
        repository.snapshot.operations,
        coverage,
        "revision-coverage-latest",
        repository.snapshot.budgets,
    )

    response = client.get("/api/v1/operations/coverage", headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    assert response.json()["data"]["items"] == [
        {
            "data_kind": "lineup",
            "availability": "available",
            "count": 1,
            "latest_observed_at": "2026-09-20T14:00:00Z",
            "evidence_codes": ["OP-005"],
        }
    ]
    detail = client.get("/api/v1/matches/match-2", headers=OPERATOR_HEADERS).json()
    assert detail["data"]["source_coverage"]["lineup"] == "available"


def test_performance_and_operations_are_server_aggregated_with_revisions() -> None:
    client = _client()

    performance = client.get("/api/v1/performance", headers=OPERATOR_HEADERS).json()
    operations = client.get("/api/v1/operations", headers=OPERATOR_HEADERS).json()
    coverage = client.get("/api/v1/operations/coverage", headers=OPERATOR_HEADERS).json()

    rows = performance["data"]["items"]
    ai = next(row for row in rows if row["provider"] == "openai")
    statistical = next(row for row in rows if row["prediction_type"] == "statistical")
    home_rate = next(row for row in rows if row["prediction_type"] == "home_rate")
    market = next(row for row in rows if row["prediction_type"] == "market")

    assert ai["sample_size"] == 1
    assert ai["metrics"] == {
        "winner_n": 1,
        "set_n": 1,
        "winner_accuracy": 1.0,
        "brier": 0.16,
        "log_loss": 0.5108256238,
        "set_rps": 0.12,
        "set_score_accuracy": 1.0,
    }
    assert ai["calibration"][0]["sample_size"] == 1
    assert ai["calibration"][0]["interval_version"] == "fixed-width-0.2-wilson-95-v1"
    comparisons = {row["baseline_kind"]: row for row in ai["comparisons"]}
    assert comparisons["statistical"]["paired_n"] == 1
    assert comparisons["statistical"]["brier"]["mean_difference"] == -0.09
    assert comparisons["statistical"]["brier"]["standard_error"] is None
    assert comparisons["statistical"]["brier"]["confidence_lower"] is None
    assert comparisons["market"]["paired_n"] == 1
    assert comparisons["home_rate"]["paired_n"] == 0
    assert statistical["sample_size"] == 1
    assert home_rate["sample_size"] == 0
    assert home_rate["metrics"]["brier"] is None
    assert market["market_availability"] == "available"
    assert market["sample_size"] == 1
    assert performance["metadata"]["evaluation_revision_ids"] == [
        "evaluation-1",
        "evaluation-stat-1",
    ]
    assert operations["data"]["items"][0]["error_code"] == "provider_timeout"
    assert operations["data"]["items"][0]["retryable"] is True
    assert operations["data"]["budgets"] == [
        {
            "provider": "openai",
            "currency": "USD",
            "period": "day",
            "period_start": "2026-09-20",
            "period_end": "2026-09-21",
            "as_of": "2026-09-20T13:31:00Z",
            "reserved_amount": "1.50000000",
            "actual_amount": "0.70000000",
            "effective_amount": "1.20000000",
            "reservation_count": 2,
            "settled_count": 1,
            "outstanding_count": 1,
            "conservative_charge_count": 0,
        },
        {
            "provider": "openai",
            "currency": "USD",
            "period": "month",
            "period_start": "2026-09-01",
            "period_end": "2026-10-01",
            "as_of": "2026-09-20T13:31:00Z",
            "reserved_amount": "3.00000000",
            "actual_amount": "2.20000000",
            "effective_amount": "2.70000000",
            "reservation_count": 4,
            "settled_count": 3,
            "outstanding_count": 1,
            "conservative_charge_count": 1,
        },
    ]
    assert coverage["data"]["items"][0]["availability"] == "missing"
    assert coverage["metadata"]["source_snapshot_ids"] == ["source-match-2"]


def test_cursor_signature_and_snapshot_revision_are_enforced() -> None:
    client = _client()
    first = client.get(
        "/api/v1/schedule?date=2026-09-20&timezone=UTC&limit=1",
        headers=OPERATOR_HEADERS,
    ).json()
    cursor = first["data"]["next_cursor"]
    assert cursor is not None

    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    invalid = client.get(
        "/api/v1/schedule",
        params={"date": "2026-09-20", "timezone": "UTC", "limit": 1, "cursor": tampered},
        headers=OPERATOR_HEADERS,
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_cursor"

    repository = client.app.state.read_repository
    repository.snapshot = ReadSnapshot(
        repository.snapshot.matches,
        repository.snapshot.predictions,
        repository.snapshot.evaluations,
        repository.snapshot.operations,
        repository.snapshot.coverage,
        "revision-2",
    )
    stale = client.get(
        "/api/v1/schedule",
        params={"date": "2026-09-20", "timezone": "UTC", "limit": 1, "cursor": cursor},
        headers=OPERATOR_HEADERS,
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "cursor_revision_stale"
    assert stale.json()["retryable"] is True


def test_filtered_performance_provenance_excludes_unselected_evaluations() -> None:
    response = _client().get("/api/v1/performance?provider=anthropic", headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    assert response.json()["data"] == {"cohort_policy_version": "none", "items": []}
    assert response.json()["metadata"]["evaluation_revision_ids"] == []


def test_performance_keeps_full_cohort_dimensions_separate() -> None:
    client = _client()
    repository = client.app.state.read_repository
    original = repository.snapshot.evaluations[0]
    metric_values = dict(original["metric_values"])
    metric_values["cohort"] = {**metric_values["cohort"], "division": "men"}
    other = {
        **original,
        "id": "evaluation-2",
        "division": "men",
        "metric_values": metric_values,
    }
    repository.snapshot = ReadSnapshot(
        repository.snapshot.matches,
        repository.snapshot.predictions,
        (*repository.snapshot.evaluations, other),
        repository.snapshot.operations,
        repository.snapshot.coverage,
        "revision-cohorts",
    )

    response = client.get("/api/v1/performance", headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    ai_items = [item for item in items if item["provider"] == "openai"]
    assert len(ai_items) == 2
    assert {item["division"] for item in ai_items} == {"men", "women"}
    assert {item["cohort"]["division"] for item in ai_items} == {"men", "women"}


def test_performance_uses_latest_corrected_revision_and_exact_paired_statistics() -> None:
    client = _client()
    repository = client.app.state.read_repository
    ai = repository.snapshot.evaluations[0]
    statistical = repository.snapshot.evaluations[1]

    corrected_metrics = {
        **ai["metric_values"],
        "brier": 0.04,
        "log_loss": 0.2231435513,
        "cohort": {
            **ai["metric_values"]["cohort"],
            "result_finality": "corrected",
        },
    }
    corrected = {
        **ai,
        "id": "evaluation-corrected",
        "result_revision_id": "result-2",
        "result_revision_number": 2,
        "result_finality": "corrected",
        "evaluation_created_at": datetime(2026, 9, 20, 18, 0, tzinfo=UTC),
        "metric_values": corrected_metrics,
    }
    second_ai = {
        **corrected,
        "id": "evaluation-ai-2",
        "prediction_id": "prediction-ai-2",
        "match_id": "match-2",
        "result_revision_id": "result-match-2",
        "schedule_revision_id": "schedule-match-2",
        "feature_snapshot_id": "feature-match-2",
        "input_cutoff_at": datetime(2026, 9, 20, 14, 30, tzinfo=UTC),
        "match_start_at": datetime(2026, 9, 20, 15, 30, tzinfo=UTC),
        "metric_values": {
            **corrected_metrics,
            "brier": 0.09,
            "log_loss": 0.3566749439,
        },
    }
    corrected_statistical = {
        **statistical,
        "id": "evaluation-stat-corrected",
        "result_revision_id": "result-2",
        "result_revision_number": 2,
        "result_finality": "corrected",
        "evaluation_created_at": datetime(2026, 9, 20, 18, 0, tzinfo=UTC),
        "metric_values": {
            **statistical["metric_values"],
            "cohort": {
                **statistical["metric_values"]["cohort"],
                "result_finality": "corrected",
            },
        },
    }
    second_statistical = {
        **statistical,
        "id": "evaluation-stat-2",
        "prediction_id": "prediction-stat-2",
        "match_id": "match-2",
        "result_revision_id": "result-match-2",
        "schedule_revision_id": "schedule-match-2",
        "feature_snapshot_id": "feature-match-2",
        "input_cutoff_at": datetime(2026, 9, 20, 14, 30, tzinfo=UTC),
        "match_start_at": datetime(2026, 9, 20, 15, 30, tzinfo=UTC),
        "metric_values": {
            **statistical["metric_values"],
            "brier": 0.36,
            "log_loss": 0.9162907319,
            "cohort": {
                **statistical["metric_values"]["cohort"],
                "result_finality": "corrected",
            },
        },
    }
    repository.snapshot = ReadSnapshot(
        repository.snapshot.matches,
        repository.snapshot.predictions,
        (
            *repository.snapshot.evaluations,
            corrected,
            corrected_statistical,
            second_ai,
            second_statistical,
        ),
        repository.snapshot.operations,
        repository.snapshot.coverage,
        "revision-corrected-performance",
        repository.snapshot.budgets,
    )

    response = client.get(
        "/api/v1/performance",
        params={"provider": "openai", "result_finality": "corrected"},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 200
    row = next(item for item in response.json()["data"]["items"] if item["provider"] == "openai")
    assert row["sample_size"] == 2
    assert row["corrected_evaluation_count"] == 2
    assert row["evaluation_revision_ids"] == ["evaluation-ai-2", "evaluation-corrected"]
    assert row["result_revision_ids"] == ["result-2", "result-match-2"]
    statistical_pair = next(
        comparison
        for comparison in row["comparisons"]
        if comparison["baseline_kind"] == "statistical"
    )
    assert statistical_pair["paired_n"] == 2
    assert statistical_pair["ai_individual_n"] == 2
    assert statistical_pair["baseline_individual_n"] == 2
    assert abs(statistical_pair["brier"]["mean_difference"] + 0.24) < 1e-12
    assert statistical_pair["brier"]["standard_error"] is not None
    assert statistical_pair["brier"]["confidence_lower"] is not None
    assert statistical_pair["brier"]["confidence_upper"] is not None
    assert "evaluation-1" not in response.json()["metadata"]["evaluation_revision_ids"]

    old = client.get(
        "/api/v1/performance",
        params={"provider": "openai", "result_finality": "final"},
        headers=OPERATOR_HEADERS,
    )
    assert old.status_code == 200
    assert old.json()["data"]["items"] == []


def test_prediction_history_uses_latest_evaluation_revision() -> None:
    client = _client()
    repository = client.app.state.read_repository
    original = repository.snapshot.evaluations[0]
    corrected = {**original, "id": "evaluation-2"}
    repository.snapshot = ReadSnapshot(
        repository.snapshot.matches,
        repository.snapshot.predictions,
        (*repository.snapshot.evaluations, corrected),
        repository.snapshot.operations,
        repository.snapshot.coverage,
        "revision-corrected",
    )

    response = client.get("/api/v1/predictions", headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["data"]["items"]}
    assert by_id["prediction-1"]["evaluation_revision_id"] == "evaluation-2"


def test_operator_retry_is_authenticated_idempotent_and_deadline_safe() -> None:
    client = _client()
    headers = {**OPERATOR_HEADERS, "Idempotency-Key": "retry-request-1"}
    first = client.post(f"/api/v1/jobs/{JOB_ID}/retry", headers=headers)
    assert first.status_code == 200
    assert first.json()["data"]["state"] == "retry_wait"
    assert first.json()["data"]["idempotent_replay"] is False

    replay = client.post(f"/api/v1/jobs/{JOB_ID}/retry", headers=headers)
    assert replay.status_code == 200
    assert replay.json()["data"]["idempotent_replay"] is True

    conflict = client.post(
        f"/api/v1/jobs/{JOB_ID}/retry",
        headers={**OPERATOR_HEADERS, "Idempotency-Key": "retry-request-2"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "job_not_retryable"

    expired = _client(now=datetime(2026, 9, 20, 13, 36, tzinfo=UTC)).post(
        f"/api/v1/jobs/{JOB_ID}/retry", headers=headers
    )
    assert expired.status_code == 409
    assert expired.json()["code"] == "retry_deadline_expired"


def test_error_contract_covers_unconfigured_auth_validation_and_database_failure() -> None:
    unconfigured = TestClient(create_app(repository=InMemoryReadRepository(), environ={}))
    auth_error = unconfigured.get("/api/v1/schedule?date=2026-09-20")
    assert auth_error.status_code == 503
    assert auth_error.json()["code"] == "operator_auth_unconfigured"
    assert auth_error.json()["retryable"] is True

    validation = _client().get("/api/v1/schedule?date=not-a-date", headers=OPERATOR_HEADERS)
    assert validation.status_code == 422
    assert validation.json()["code"] == "validation_error"

    class UnavailableRepository(InMemoryReadRepository):
        def load(self) -> ReadSnapshot:
            raise SQLAlchemyError("private database detail")

    unavailable = TestClient(
        create_app(
            repository=UnavailableRepository(),
            environ={"VLYTICS_OPERATOR_AUTH_SECRET": "synthetic-operator-secret"},
        )
    ).get("/api/v1/schedule?date=2026-09-20", headers=OPERATOR_HEADERS)
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "service_unavailable"
    assert "private database detail" not in str(unavailable.json())


def test_operator_retry_migration_is_restricted_and_deadline_checked() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "migrations" / "0014_operator_retry.sql"
    ).read_text(encoding="utf-8")
    assert "SECURITY DEFINER" in migration
    assert "GRANT EXECUTE ON FUNCTION ops.request_job_retry" in migration
    assert "REVOKE ALL PRIVILEGES ON FUNCTION ops.request_job_retry" in migration
    assert "target_job.deadline_at <= requested_at" in migration
    assert "OLD.state = 'failed' AND NEW.state = 'retry_wait'" in migration


def test_postgres_read_queries_match_migrated_schema(postgres_role_urls: object) -> None:
    database_url = str(postgres_role_urls.read_api)
    engine = create_engine(database_url)
    try:
        snapshot = PostgresReadRepository(engine).load()
        assert isinstance(snapshot.revision, str)
    finally:
        engine.dispose()


def test_postgres_endpoint_scoped_queries_match_migrated_schema(
    postgres_role_urls: object,
) -> None:
    engine = create_engine(str(postgres_role_urls.read_api))
    boundary = (datetime(2026, 9, 20, 13, 30, tzinfo=UTC), str(uuid4()))
    queries = (
        ReadQuery(
            endpoint="schedule",
            filters={
                "start_at": datetime(2026, 9, 20, tzinfo=UTC),
                "end_at": datetime(2026, 9, 21, tzinfo=UTC),
                "division": "women",
                "team": None,
                "competition": None,
            },
        ),
        ReadQuery(endpoint="match", filters={"match_id": str(uuid4())}),
        ReadQuery(
            endpoint="predictions",
            filters={
                "division": "women",
                "team": None,
                "competition": None,
                "provider": "openai",
                "model": None,
                "prediction_type": None,
                "prompt_version": None,
                "start_at": None,
                "end_at": None,
            },
            boundary=boundary,
            limit=25,
        ),
        ReadQuery(
            endpoint="performance",
            filters={
                "division": "women",
                "competition": None,
                "stage": None,
                "feature_version": None,
                "availability_policy": None,
                "timing_eligibility": None,
                "evaluator_version": None,
                "start_at": None,
                "end_at": None,
            },
        ),
        ReadQuery(
            endpoint="operations",
            filters={"state": "failed", "job_type": None},
            boundary=boundary,
            limit=25,
        ),
        ReadQuery(
            endpoint="coverage",
            filters={"data_kind": "lineup", "availability": "available"},
        ),
    )
    try:
        snapshots = [PostgresReadRepository(engine).load_query(query) for query in queries]
        assert all(isinstance(snapshot.revision, str) for snapshot in snapshots)
        assert snapshots[2].scoped_page is True
        assert snapshots[4].scoped_page is True
    finally:
        engine.dispose()


def test_postgres_operations_query_uses_filtered_keyset_page(
    postgres_role_urls: object,
) -> None:
    engine = create_engine(str(postgres_role_urls.migrator))
    repository = PostgresReadRepository(engine)
    base_due = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    job_ids = [uuid4() for _ in range(4)]
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    text(
                        """
                        INSERT INTO ops.jobs (
                            id, job_key, job_type, due_at, state, error_code
                        ) VALUES (
                            :id, :job_key, :job_type, :due_at, :state, :error_code
                        )
                        """
                    ),
                    [
                        {
                            "id": identifier,
                            "job_key": f"api-keyset:{identifier}",
                            "job_type": "synthetic.keyset",
                            "due_at": base_due + timedelta(minutes=index),
                            "state": "failed" if index < 3 else "queued",
                            "error_code": "synthetic_failure" if index < 3 else None,
                        }
                        for index, identifier in enumerate(job_ids)
                    ],
                )
                query = ReadQuery(
                    endpoint="operations",
                    filters={"state": "failed", "job_type": "synthetic.keyset"},
                    limit=2,
                )
                first = repository._load_query(connection, query)
                assert [row["id"] for row in first.operations] == [
                    str(job_ids[0]),
                    str(job_ids[1]),
                ]
                assert first.has_more is True

                second = repository._load_query(
                    connection,
                    ReadQuery(
                        endpoint="operations",
                        filters=query.filters,
                        boundary=(base_due + timedelta(minutes=1), str(job_ids[1])),
                        limit=2,
                    ),
                )
                assert [row["id"] for row in second.operations] == [str(job_ids[2])]
                assert second.has_more is False
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_postgres_operator_retry_function_is_idempotent_and_role_restricted(
    postgres_role_urls: object,
) -> None:
    job_id = uuid4()
    requested_at = datetime(2026, 9, 20, 13, 30, tzinfo=UTC)
    engine = create_engine(postgres_role_urls.engine)
    read_engine = create_engine(postgres_role_urls.read_api)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO ops.jobs (
                        id, job_key, job_type, due_at, deadline_at, state, error_code
                    ) VALUES (
                        :id, :job_key, 'synthetic.retry', :due_at, :deadline_at,
                        'failed', 'provider_timeout'
                    )
                    """
                ),
                {
                    "id": job_id,
                    "job_key": f"api-retry-test:{job_id}",
                    "due_at": requested_at,
                    "deadline_at": datetime(2026, 9, 20, 13, 35, tzinfo=UTC),
                },
            )
        repository = PostgresReadRepository(read_engine)
        first = repository.request_retry(
            job_id=str(job_id),
            idempotency_key=f"api-retry-request:{job_id}",
            requested_at=requested_at,
        )
        replay = repository.request_retry(
            job_id=str(job_id),
            idempotency_key=f"api-retry-request:{job_id}",
            requested_at=requested_at,
        )
        assert first["state"] == "retry_wait"
        assert first["idempotent_replay"] is False
        assert replay["idempotent_replay"] is True
    finally:
        read_engine.dispose()
        engine.dispose()


def test_postgres_performance_uses_market_evaluation_snapshot(
    postgres_role_urls: object,
) -> None:
    run_id = f"api-market-{uuid4().hex}"
    manifest = _seed_postgres_graph(postgres_role_urls, run_id)
    engine = create_engine(str(postgres_role_urls.migrator))
    market_snapshot_id = uuid4()
    source_event_id = f"market-{uuid4().hex}"
    now = datetime.now(UTC)
    try:
        with engine.begin() as connection:
            prediction_id = connection.execute(
                text(
                    """
                    SELECT p.id FROM engine.predictions p
                    JOIN engine.model_variants v ON v.id = p.variant_id
                    WHERE p.match_id = CAST(:match_id AS uuid) AND v.provider = 'openai'
                    """
                ),
                {"match_id": manifest["match_id"]},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO market.source_event_mappings (source, source_event_id, match_id)
                    VALUES ('synthetic-api-test', :source_event_id, CAST(:match_id AS uuid))
                    """
                ),
                {"source_event_id": source_event_id, "match_id": manifest["match_id"]},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO market.market_snapshots (
                        id, match_id, source, source_event_id, quoted_at, observed_at,
                        received_at, contract_version, markets_json, sha256
                    ) VALUES (
                        :id, CAST(:match_id AS uuid), 'synthetic-api-test', :source_event_id,
                        :now, :now, :now, 'api-test-v1', CAST(:markets AS jsonb), :sha256
                    )
                    """
                ),
                {
                    "id": market_snapshot_id,
                    "match_id": manifest["match_id"],
                    "source_event_id": source_event_id,
                    "now": now,
                    "markets": json.dumps(
                        {
                            "markets": [
                                {
                                    "market_type": "moneyline",
                                    "period": "full_match",
                                    "selection": "home",
                                    "decimal_odds": "1.80",
                                },
                                {
                                    "market_type": "moneyline",
                                    "period": "full_match",
                                    "selection": "away",
                                    "decimal_odds": "2.20",
                                },
                            ]
                        }
                    ),
                    "sha256": uuid4().hex + uuid4().hex,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO market.market_evaluations (
                        match_id, prediction_id, market_snapshot_id, evaluator_version,
                        derived_probabilities, eligibility, reason
                    ) VALUES (
                        CAST(:match_id AS uuid), :prediction_id, :market_snapshot_id,
                        'api-market-v1', '{}'::jsonb, 'eligible', 'as_of_cutoff'
                    )
                    """
                ),
                {
                    "match_id": manifest["match_id"],
                    "prediction_id": prediction_id,
                    "market_snapshot_id": market_snapshot_id,
                },
            )
    finally:
        engine.dispose()

    client = _postgres_client(postgres_role_urls)
    response = client.get(
        "/api/v1/performance",
        params={"competition": manifest["competition"], "provider": "openai"},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    market = next(row for row in item["comparisons"] if row["baseline_kind"] == "market")
    assert market["baseline_individual_n"] == 1
    assert market["paired_n"] == 1


def test_postgres_history_cursor_ignores_jobs_and_tracks_lifecycle(
    postgres_role_urls: object,
) -> None:
    run_id = f"api-cursor-{uuid4().hex}"
    manifest = _seed_postgres_graph(postgres_role_urls, run_id)
    client = _postgres_client(postgres_role_urls)
    first = client.get(
        "/api/v1/predictions",
        params={"competition": manifest["competition"], "limit": 1},
        headers=OPERATOR_HEADERS,
    )
    assert first.status_code == 200
    cursor = first.json()["data"]["next_cursor"]
    assert cursor is not None

    engine = create_engine(str(postgres_role_urls.migrator))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO ops.jobs (job_key, job_type, due_at)
                    VALUES (:job_key, 'unrelated.cursor-change', :due_at)
                    """
                ),
                {"job_key": f"unrelated:{uuid4()}", "due_at": datetime.now(UTC)},
            )
        second = client.get(
            "/api/v1/predictions",
            params={
                "competition": manifest["competition"],
                "limit": 1,
                "cursor": cursor,
            },
            headers=OPERATOR_HEADERS,
        )
        assert second.status_code == 200
        assert len(second.json()["data"]["items"]) == 1

        with engine.begin() as connection:
            prediction = (
                connection.execute(
                    text(
                        """
                    SELECT p.id, p.schedule_revision_id
                    FROM engine.predictions p
                    JOIN engine.model_variants v ON v.id = p.variant_id
                    WHERE p.match_id = CAST(:match_id AS uuid) AND v.provider = 'openai'
                    """
                    ),
                    {"match_id": manifest["match_id"]},
                )
                .mappings()
                .one()
            )
            event_id = uuid4()
            now = datetime.now(UTC) + timedelta(seconds=1)
            connection.execute(
                text(
                    """
                    INSERT INTO engine.prediction_events (
                        id, prediction_id, event_type, reason, occurred_at,
                        observed_at, schedule_revision_id
                    ) VALUES (
                        :id, :prediction_id, 'voided', 'api_cursor_regression',
                        :now, :now, :schedule_revision_id
                    )
                    """
                ),
                {
                    "id": event_id,
                    "prediction_id": prediction["id"],
                    "now": now,
                    "schedule_revision_id": prediction["schedule_revision_id"],
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE engine.prediction_status_projection
                    SET latest_event_id = :event_id, current_status = 'voided', refreshed_at = :now
                    WHERE prediction_id = :prediction_id
                    """
                ),
                {"event_id": event_id, "now": now, "prediction_id": prediction["id"]},
            )
        stale = client.get(
            "/api/v1/predictions",
            params={
                "competition": manifest["competition"],
                "limit": 1,
                "cursor": cursor,
            },
            headers=OPERATOR_HEADERS,
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "cursor_revision_stale"
    finally:
        engine.dispose()


def test_postgres_history_team_keyset_uses_prediction_schedule_revision(
    postgres_role_urls: object,
) -> None:
    run_id = f"api-team-{uuid4().hex}"
    manifest = _seed_postgres_graph(postgres_role_urls, run_id)
    engine = create_engine(str(postgres_role_urls.migrator))
    try:
        with engine.begin() as connection:
            match = (
                connection.execute(
                    text(
                        """
                    SELECT m.id, m.source, m.season_id, m.raw_snapshot_id,
                           mr.scheduled_start_at, mr.actual_start_at
                    FROM mirror.matches m
                    JOIN mirror.match_revisions mr ON mr.match_id = m.id
                    WHERE m.id = CAST(:match_id AS uuid)
                    ORDER BY mr.revision DESC LIMIT 1
                    """
                    ),
                    {"match_id": manifest["match_id"]},
                )
                .mappings()
                .one()
            )
            replacement_ids = [uuid4(), uuid4()]
            for team_id, code in zip(replacement_ids, ("NEW-HOME", "NEW-AWAY"), strict=True):
                connection.execute(
                    text(
                        """
                        INSERT INTO mirror.team_identities (
                            id, source, source_team_code, season_id, display_name,
                            observed_at, mapping_version, raw_snapshot_id
                        ) VALUES (
                            :id, :source, :code, :season_id, :code,
                            :now, 'api-team-v2', :raw_snapshot_id
                        )
                        """
                    ),
                    {
                        "id": team_id,
                        "source": match["source"],
                        "code": f"{code}-{run_id}",
                        "season_id": match["season_id"],
                        "now": datetime.now(UTC),
                        "raw_snapshot_id": match["raw_snapshot_id"],
                    },
                )
            connection.execute(
                text(
                    """
                    INSERT INTO mirror.match_revisions (
                        match_id, revision, home_team_id, away_team_id,
                        scheduled_start_at, actual_start_at, status, observed_at, raw_snapshot_id
                    ) VALUES (
                        :match_id, 2, :home_team_id, :away_team_id,
                        :scheduled_start_at, :actual_start_at, 'finished', :now, :raw_snapshot_id
                    )
                    """
                ),
                {
                    "match_id": match["id"],
                    "home_team_id": replacement_ids[0],
                    "away_team_id": replacement_ids[1],
                    "scheduled_start_at": match["scheduled_start_at"],
                    "actual_start_at": match["actual_start_at"],
                    "now": datetime.now(UTC),
                    "raw_snapshot_id": match["raw_snapshot_id"],
                },
            )
    finally:
        engine.dispose()

    client = _postgres_client(postgres_role_urls)
    params = {"team": f"LIVE-HOME-{run_id}", "limit": 1}
    first = client.get("/api/v1/predictions", params=params, headers=OPERATOR_HEADERS)
    assert first.status_code == 200
    assert len(first.json()["data"]["items"]) == 1
    cursor = first.json()["data"]["next_cursor"]
    assert cursor is not None
    second = client.get(
        "/api/v1/predictions",
        params={**params, "cursor": cursor},
        headers=OPERATOR_HEADERS,
    )
    assert second.status_code == 200
    assert len(second.json()["data"]["items"]) == 1


def test_postgres_performance_companions_stay_within_filtered_evaluation_matches(
    postgres_role_urls: object,
) -> None:
    selected_run = f"api-performance-selected-{uuid4().hex}"
    unrelated_run = f"api-performance-unrelated-{uuid4().hex}"
    selected = _seed_postgres_graph(postgres_role_urls, selected_run)
    unrelated = _seed_postgres_graph(postgres_role_urls, unrelated_run)
    day = datetime.fromisoformat(str(selected["schedule_date"]))
    start_at = day.replace(tzinfo=UTC)
    end_at = start_at + timedelta(days=1)
    engine = create_engine(str(postgres_role_urls.read_api))
    try:
        snapshot = PostgresReadRepository(engine).load_query(
            ReadQuery(
                endpoint="performance",
                filters={
                    "division": "women",
                    "competition": selected["competition"],
                    "stage": "regular",
                    "feature_version": "live-feature-v1",
                    "availability_policy": "live_prospective",
                    "timing_eligibility": "on_time",
                    "evaluator_version": "live-evaluator-v1",
                    "provider": "openai",
                    "model": "live-openai-model",
                    "prediction_type": "winner",
                    "prompt_version": "live-prompt-v1",
                    "result_finality": "final",
                    "start_at": start_at,
                    "end_at": end_at,
                },
            )
        )
    finally:
        engine.dispose()

    assert {row["match_id"] for row in snapshot.evaluations} == {selected["match_id"]}
    assert {row["match_id"] for row in snapshot.predictions} == {selected["match_id"]}
    assert {row["provider"] for row in snapshot.predictions} == {"openai", "statistical"}
    assert unrelated["match_id"] not in {row["match_id"] for row in snapshot.predictions}
