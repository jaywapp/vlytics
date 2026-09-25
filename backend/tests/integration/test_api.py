from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from vlytics.api.app import create_app
from vlytics.api.repository import InMemoryReadRepository, PostgresReadRepository, ReadSnapshot

OPERATOR_HEADERS = {"Authorization": "Bearer synthetic-operator-secret"}
JOB_ID = "11111111-1111-1111-1111-111111111111"


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
) -> dict[str, object]:
    return {
        "id": identifier,
        "match_id": match_id,
        "competition": "regular-2026",
        "division": "women",
        "provider": provider,
        "prediction_type": "winner",
        "requested_model": f"{provider}-synthetic",
        "resolved_model_id": f"{provider}-synthetic-2026-09",
        "model_version": "2026-09",
        "prompt_version": "prompt-v1",
        "feature_version": "feature-v2",
        "schedule_revision_id": f"schedule-{match_id}",
        "source_snapshot_id": f"source-{match_id}",
        "feature_snapshot_id": f"feature-{match_id}",
        "generated_at": generated_at,
        "status": "published",
        "provider_status": status,
        "error_code": "provider_timeout" if status != "succeeded" else None,
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
        "reason": "market_source_not_configured_or_no_eligible_quote",
    }
    assert detail["data"]["provider_outcomes"][0]["status"] == "timed_out"
    assert detail["data"]["provider_outcomes"][0]["error_code"] == "provider_timeout"
    assert "raw" not in str(detail).lower()


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
