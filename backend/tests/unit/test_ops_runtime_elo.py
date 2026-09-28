"""Production statistical runner coverage for the point-in-time Elo source."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from math import isclose
from typing import Any
from uuid import UUID

import pytest

from vlytics.engine.features import (
    AvailabilityPolicy,
    FeatureLineage,
    FeatureSnapshot,
    LineupStatus,
)
from vlytics.engine.predictors import (
    Division,
    EloMatch,
    EloResultRevision,
    FranchiseIdentity,
    ResultFinality,
    ResultFinalityPolicy,
    TimingEligibility,
    reproduction_candidate,
)
from vlytics.engine.predictors.elo import replay_elo_prediction
from vlytics.ops.runtime import (
    STATISTICAL_MODEL_VERSION,
    DatabaseStatisticalPredictionRunner,
    StatisticalMatchMetadata,
)
from vlytics.ops.scheduler import TerminalJobError

START = datetime(2030, 1, 1, tzinfo=UTC)
TARGET_MATCH_ID = "00000000-0000-0000-0000-000000000003"
TARGET_SCHEDULE_ID = "00000000-0000-0000-0000-000000000013"
SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000023")
VARIANT_ID = UUID("00000000-0000-0000-0000-000000000033")
MAPPING_VERSION = "verified-franchise-v1"


def _identity(franchise_id: str) -> FranchiseIdentity:
    return FranchiseIdentity(franchise_id, MAPPING_VERSION, "reviewed source records")


def _match(
    *,
    match_id: str,
    schedule_revision_id: str,
    cutoff_at: datetime,
    season_id: str,
    home_team_id: str,
    away_team_id: str,
    result: EloResultRevision | None = None,
) -> EloMatch:
    return EloMatch(
        match_id=match_id,
        schedule_revision_id=schedule_revision_id,
        schedule_revision=1,
        schedule_observed_at=cutoff_at - timedelta(days=1),
        schedule_raw_snapshot_sha256="a" * 64,
        prediction_cutoff_at=cutoff_at,
        scheduled_start_at=cutoff_at + timedelta(hours=1),
        season_id=season_id,
        competition="vleague",
        stage="regular",
        division=Division.MEN,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        availability_policy=AvailabilityPolicy.LIVE_PROSPECTIVE,
        timing_eligibility=TimingEligibility.ON_TIME,
        result_finality_policy=ResultFinalityPolicy.FINAL_ONLY,
        franchise_mapping_version=MAPPING_VERSION,
        result_revisions=(result,) if result else (),
        home_franchise=_identity("00000000-0000-0000-0000-000000000101"),
        away_franchise=_identity("00000000-0000-0000-0000-000000000102"),
    )


def _timeline() -> tuple[EloMatch, ...]:
    first_match_id = "00000000-0000-0000-0000-000000000001"
    first_result = EloResultRevision(
        match_id=first_match_id,
        revision_id="00000000-0000-0000-0000-000000000021",
        revision=1,
        observed_at=START + timedelta(hours=3),
        finalized_at=START + timedelta(hours=3),
        raw_snapshot_sha256="b" * 64,
        finality=ResultFinality.FINAL,
        home_sets=3,
        away_sets=0,
    )
    return (
        _match(
            match_id=first_match_id,
            schedule_revision_id="00000000-0000-0000-0000-000000000011",
            cutoff_at=START,
            season_id="season-1",
            home_team_id="home-season-1",
            away_team_id="away-season-1",
            result=first_result,
        ),
        _match(
            match_id=TARGET_MATCH_ID,
            schedule_revision_id=TARGET_SCHEDULE_ID,
            cutoff_at=START + timedelta(days=1),
            season_id="season-2",
            home_team_id="home-season-2",
            away_team_id="away-season-2",
        ),
    )


def _snapshot() -> FeatureSnapshot:
    cutoff = START + timedelta(days=1)
    return FeatureSnapshot(
        feature_version="feature-v1",
        availability_policy=AvailabilityPolicy.LIVE_PROSPECTIVE,
        target_match_id=TARGET_MATCH_ID,
        schedule_revision_id=TARGET_SCHEDULE_ID,
        cutoff_at=cutoff,
        captured_at=cutoff,
        lineup_status=LineupStatus.UNKNOWN,
        team_features={},
        matchup_features={},
        players={},
        lineage=FeatureLineage(),
        sha256="c" * 64,
    )


def _metadata() -> StatisticalMatchMetadata:
    cutoff = START + timedelta(days=1)
    return StatisticalMatchMetadata(
        source="kovo",
        source_group_code="vleague",
        season_id="season-2",
        competition="vleague",
        stage="regular",
        division=Division.MEN,
        scheduled_start_at=cutoff + timedelta(hours=1),
        schedule_observed_at=cutoff - timedelta(days=1),
        schedule_revision=1,
        schedule_raw_snapshot_sha256="a" * 64,
    )


class _Engine:
    def connect(self) -> Any:
        return nullcontext(object())


class _EloRunner(DatabaseStatisticalPredictionRunner):
    @staticmethod
    def _metadata(connection: object, snapshot: FeatureSnapshot) -> StatisticalMatchMetadata:
        del connection, snapshot
        return _metadata()

    @staticmethod
    def _elo_replay(
        connection: object,
        snapshot: FeatureSnapshot,
        metadata: StatisticalMatchMetadata,
    ) -> Any:
        del connection, metadata
        return replay_elo_prediction(
            _timeline(),
            reproduction_candidate(),
            target_match_id=snapshot.target_match_id,
        )

    @staticmethod
    def _fifth_set_history(
        connection: object,
        snapshot: FeatureSnapshot,
        *,
        eligible_result_ids: tuple[str, ...],
    ) -> tuple[int, int, tuple[str, ...]]:
        del connection, snapshot, eligible_result_ids
        return (0, 0, ())

    @staticmethod
    def _verified_rule(
        connection: object,
        snapshot: FeatureSnapshot,
        metadata: StatisticalMatchMetadata,
        *,
        eligible_result_ids: tuple[str, ...],
    ) -> None:
        del connection, snapshot, metadata, eligible_result_ids
        return None


def test_production_runner_uses_versioned_elo_with_home_and_season_carryover() -> None:
    runner = _EloRunner(_Engine(), variant_id=VARIANT_ID, variant_key="statistical-joint-v1")
    result = runner.predict(snapshot_id=SNAPSHOT_ID, snapshot=_snapshot())

    initial_probability = 1.0 / (1.0 + 10.0 ** (-15.0 / 400.0))
    rating_change = 32.0 * 1.25 * (1.0 - initial_probability)
    home_rating = 1500.0 + 0.5 * rating_change
    away_rating = 1500.0 - 0.5 * rating_change
    expected = 1.0 / (1.0 + 10.0 ** ((away_rating - home_rating - 15.0) / 400.0))

    assert result.resolved_model_id == STATISTICAL_MODEL_VERSION == "elo-p5-joint-v2"
    assert result.output["home_win_probability"] == pytest.approx(expected)
    assert not isclose(float(result.output["home_win_probability"]), 0.5)
    winner = result.output["target_provenance"]["winner"]
    assert winner["upstream_model_version"] == "elo-home-win-v2"
    assert winner["upstream_config_id"] == reproduction_candidate().config_id


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def __iter__(self) -> Any:
        return iter(self._rows)


class _TimelineConnection:
    def execute(self, statement: object, parameters: object) -> _Rows:
        del statement, parameters
        cutoff = START + timedelta(days=1)
        return _Rows(
            [
                {
                    "match_id": TARGET_MATCH_ID,
                    "season_id": "season-2",
                    "schedule_revision_id": TARGET_SCHEDULE_ID,
                    "schedule_revision": 1,
                    "schedule_observed_at": cutoff - timedelta(days=1),
                    "scheduled_start_at": cutoff + timedelta(hours=1),
                    "prediction_cutoff_at": cutoff,
                    "home_team_id": "home-season-2",
                    "away_team_id": "away-season-2",
                    "schedule_raw_sha256": "a" * 64,
                    "home_franchise_id": None,
                    "home_mapping_version": None,
                    "home_evidence": None,
                    "away_franchise_id": None,
                    "away_mapping_version": None,
                    "away_evidence": None,
                    "result_revision_id": None,
                    "result_revision": None,
                    "result_observed_at": None,
                    "result_finality": None,
                    "home_sets": None,
                    "away_sets": None,
                    "result_raw_sha256": None,
                }
            ]
        )


def test_production_elo_fails_closed_without_verified_franchise_identity() -> None:
    with pytest.raises(TerminalJobError, match="statistical_elo_inputs_ineligible"):
        DatabaseStatisticalPredictionRunner._elo_replay(
            _TimelineConnection(),
            _snapshot(),
            _metadata(),
        )
