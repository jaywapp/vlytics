"""Production Feature input acceptance tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from vlytics.engine.features import (
    VERIFIED_METRIC_SCHEMA_VERSION,
    AsOfFeatureBuilder,
    AvailabilityPolicy,
    FeatureInputRepository,
)
from vlytics.mirror.franchises import FranchiseKey, FranchiseMappings
from vlytics.mirror.models import (
    Availability,
    FranchiseAssignment,
    RawReceipt,
    SourceEndpoint,
    SourceRequestKey,
    SourceResponse,
)
from vlytics.mirror.parser import KovoParser
from vlytics.mirror.rules import SeasonRuleKey, SeasonRules, VolleyballSetRule

CUTOFF = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
TARGET_ID = UUID("00000000-0000-0000-0000-000000000001")
TARGET_SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000000002")
SEASON_ID = UUID("00000000-0000-0000-0000-000000000003")
COMPETITION_ID = UUID("00000000-0000-0000-0000-000000000004")
HOME_ID = UUID("00000000-0000-0000-0000-000000000005")
AWAY_ID = UUID("00000000-0000-0000-0000-000000000006")
OTHER_ID = UUID("00000000-0000-0000-0000-000000000007")
HISTORY_ID = UUID("00000000-0000-0000-0000-000000000008")
HISTORY_SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000000009")
RESULT_ID = UUID("00000000-0000-0000-0000-000000000010")
PLAYER_ID = UUID("00000000-0000-0000-0000-000000000011")
UNVERIFIED_PLAYER_ID = UUID("00000000-0000-0000-0000-000000000012")
ROSTER_RAW_ID = UUID("00000000-0000-0000-0000-000000000013")


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _Connection:
    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: object, parameters: dict[str, Any]) -> _Rows:
        sql = str(statement)
        self.calls.append((sql, parameters))
        for marker, rows in self._responses.items():
            if f"/* feature-input:{marker} */" in sql:
                return _Rows(rows)
        raise AssertionError(f"unexpected feature input query: {sql}")


def _responses() -> dict[str, list[dict[str, Any]]]:
    history_start = CUTOFF - timedelta(days=3)
    result_observed = history_start + timedelta(hours=2)
    verified_metrics = {
        "attack_kills": 15,
        "attack_errors": 2,
        "attack_attempts": 30,
        "serve_aces": 3,
        "serve_attempts": 20,
        "block_points": 4,
        "receive_successes": 12,
        "receive_attempts": 24,
        "errors": 5,
    }
    return {
        "target": [
            {
                "id": TARGET_ID,
                "season_id": SEASON_ID,
                "competition_id": COMPETITION_ID,
                "schedule_revision_id": TARGET_SCHEDULE_ID,
                "scheduled_start_at": CUTOFF + timedelta(hours=1),
                "observed_at": CUTOFF - timedelta(hours=1),
                "home_team_id": HOME_ID,
                "away_team_id": AWAY_ID,
            }
        ],
        "history": [
            {
                "id": HISTORY_ID,
                "season_id": SEASON_ID,
                "competition_id": COMPETITION_ID,
                "schedule_id": HISTORY_SCHEDULE_ID,
                "schedule_revision": 1,
                "schedule_observed_at": history_start - timedelta(days=1),
                "scheduled_start_at": history_start,
                "home_team_id": HOME_ID,
                "away_team_id": OTHER_ID,
                "schedule_sha256": "1" * 64,
                "result_id": RESULT_ID,
                "result_revision": 1,
                "result_observed_at": result_observed,
                "home_sets": 3,
                "away_sets": 0,
                "finality": "final",
                "result_sha256": "2" * 64,
            }
        ],
        "sets": [
            {
                "result_revision_id": RESULT_ID,
                "set_number": number,
                "home_points": 25,
                "away_points": 20,
            }
            for number in range(1, 4)
        ],
        "team-stats": [
            {
                "result_revision_id": RESULT_ID,
                "team_identity_id": HOME_ID,
                "metric_schema_version": VERIFIED_METRIC_SCHEMA_VERSION,
                "metrics_json": verified_metrics,
                "coverage_availability": "available",
            },
            {
                "result_revision_id": RESULT_ID,
                "team_identity_id": OTHER_ID,
                "metric_schema_version": "kovo-observed-match-metrics-v1",
                "metrics_json": {"attack_attempts": 99},
                "coverage_availability": "available",
            },
            {
                "result_revision_id": RESULT_ID,
                "team_identity_id": AWAY_ID,
                "metric_schema_version": VERIFIED_METRIC_SCHEMA_VERSION,
                "metrics_json": verified_metrics,
                "coverage_availability": "unverified",
            },
        ],
        "player-stats": [
            {
                "result_revision_id": RESULT_ID,
                "player_id": PLAYER_ID,
                "team_identity_id": HOME_ID,
                "metric_schema_version": VERIFIED_METRIC_SCHEMA_VERSION,
                "metrics_json": {"attack_attempts": 10},
                "coverage_availability": "available",
            },
            {
                "result_revision_id": RESULT_ID,
                "player_id": UNVERIFIED_PLAYER_ID,
                "team_identity_id": HOME_ID,
                "metric_schema_version": "kovo-observed-match-metrics-v1",
                "metrics_json": {"attack_attempts": 20},
                "coverage_availability": "available",
            },
        ],
        "historical-rosters": [
            {
                "result_revision_id": RESULT_ID,
                "team_identity_id": HOME_ID,
                "player_id": PLAYER_ID,
                "coverage_availability": "available",
            },
            {
                "result_revision_id": RESULT_ID,
                "team_identity_id": HOME_ID,
                "player_id": UNVERIFIED_PLAYER_ID,
                "coverage_availability": "unverified",
            },
        ],
        "target-rosters": [
            {
                "team_identity_id": HOME_ID,
                "player_id": PLAYER_ID,
                "observed_at": CUTOFF - timedelta(minutes=5),
                "raw_snapshot_id": ROSTER_RAW_ID,
                "raw_sha256": "3" * 64,
                "coverage_availability": "available",
            },
            {
                "team_identity_id": AWAY_ID,
                "player_id": UNVERIFIED_PLAYER_ID,
                "observed_at": CUTOFF - timedelta(minutes=5),
                "raw_snapshot_id": ROSTER_RAW_ID,
                "raw_sha256": "3" * 64,
                "coverage_availability": "unverified",
            },
        ],
    }


def test_only_verified_cutoff_eligible_roster_and_stats_reach_features() -> None:
    connection = _Connection(_responses())
    inputs = FeatureInputRepository(connection).load(
        match_id=TARGET_ID,
        schedule_revision_id=TARGET_SCHEDULE_ID,
        cutoff_at=CUTOFF,
    )

    result = inputs.matches[0].result_revisions[0]
    assert set(result.team_stats) == {str(HOME_ID)}
    assert set(result.player_stats) == {str(PLAYER_ID)}
    assert result.roster_player_ids == {str(HOME_ID): (str(PLAYER_ID),)}
    assert len(inputs.roster_revisions) == 1
    assert inputs.roster_revisions[0].player_ids == (str(PLAYER_ID),)

    snapshot = AsOfFeatureBuilder().build(
        target=inputs.target,
        cutoff_at=CUTOFF,
        captured_at=CUTOFF + timedelta(seconds=1),
        policy=AvailabilityPolicy.LIVE_PROSPECTIVE,
        matches=inputs.matches,
        roster_revisions=inputs.roster_revisions,
    )
    attack = snapshot.team_features["home"]["attack_efficiency"]
    assert attack.value == pytest.approx(13 / 30)
    assert attack.lineage.metric_schema_versions == (VERIFIED_METRIC_SCHEMA_VERSION,)
    assert set(snapshot.players) == {str(PLAYER_ID)}
    assert snapshot.lineage.roster_revision_ids == (f"{ROSTER_RAW_ID}:{HOME_ID}",)

    parameter_sets = [parameters for _, parameters in connection.calls]
    assert all(
        parameters.get("cutoff_at") == CUTOFF
        for parameters in parameter_sets
        if "cutoff_at" in parameters
    )
    queries = {
        marker: sql
        for sql, _ in connection.calls
        for marker in _responses()
        if f"/* feature-input:{marker} */" in sql
    }
    assert "latest_results" in queries["history"]
    assert queries["history"].index("ORDER BY rr.match_id") < queries["history"].index(
        "coverage.availability = 'available'"
    )
    for marker, source_alias in (
        ("team-stats", "stats"),
        ("player-stats", "stats"),
        ("historical-rosters", "roster"),
    ):
        query = queries[marker]
        assert query.index("LIMIT 1") < query.index(
            f"coverage.raw_snapshot_id = {source_alias}.raw_snapshot_id"
        )


def test_invalid_or_missing_set_rows_do_not_become_zero_history() -> None:
    responses = _responses()
    responses["sets"] = []
    inputs = FeatureInputRepository(_Connection(responses)).load(
        match_id=TARGET_ID,
        schedule_revision_id=TARGET_SCHEDULE_ID,
        cutoff_at=CUTOFF,
    )

    assert inputs.matches == ()
    snapshot = AsOfFeatureBuilder().build(
        target=inputs.target,
        cutoff_at=CUTOFF,
        captured_at=CUTOFF + timedelta(seconds=1),
        policy=AvailabilityPolicy.LIVE_PROSPECTIVE,
        matches=inputs.matches,
        roster_revisions=inputs.roster_revisions,
    )
    season_rate = snapshot.team_features["home"]["season_win_rate"]
    assert season_rate.value is None
    assert season_rate.missing_reason is not None
    assert season_rate.missing_reason.value == "no_prior_matches"


def test_kovo_abbreviated_stats_and_detail_roster_remain_unverified() -> None:
    fixture = (
        Path(__file__).resolve().parents[3] / "fixtures" / "synthetic" / "kovo-game-detail.v1.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    observed_at = datetime(2099, 11, 2, 0, 0, tzinfo=UTC)
    mappings = FranchiseMappings(
        {
            FranchiseKey("kovo", "001", "999", code): FranchiseAssignment(
                UUID(f"00000000-0000-0000-0000-0000000000{suffix}"),
                "reviewed-identity-v1",
                f"synthetic evidence for {code}",
            )
            for code, suffix in (("W001", "21"), ("W002", "22"))
        }
    )
    rules = SeasonRules(
        {
            SeasonRuleKey("kovo", "001", "999"): VolleyballSetRule(
                "synthetic-rules-v1", "synthetic rule evidence"
            )
        }
    )
    receipt = RawReceipt(
        UUID("00000000-0000-0000-0000-000000000023"),
        SourceResponse(
            source="kovo",
            request_key=SourceRequestKey("001", SourceEndpoint.GAME_DETAIL, "999", "201", "2"),
            redacted_url="/stat/synthetic",
            requested_at=observed_at - timedelta(seconds=1),
            received_at=observed_at,
            status_code=200,
            body_bytes=body,
        ),
        hashlib.sha256(body).hexdigest(),
        "kovo-v1",
    )

    batch = KovoParser(mappings, rules).parse(receipt)
    coverage = {item.data_kind: item.availability for item in batch.coverage}

    assert coverage["match_result"] is Availability.AVAILABLE
    assert coverage["team_match_stats"] is Availability.UNVERIFIED
    assert coverage["player_match_stats"] is Availability.UNVERIFIED
    assert coverage["roster"] is Availability.UNVERIFIED
