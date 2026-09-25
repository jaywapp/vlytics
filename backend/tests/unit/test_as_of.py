"""Hand-calculated tests for leakage-safe feature snapshots."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from vlytics.engine.features import (
    FEATURE_DEFINITIONS,
    VERIFIED_METRIC_SCHEMA_VERSION,
    AsOfFeatureBuilder,
    AvailabilityPolicy,
    FeatureSnapshotRepository,
    FeatureSnapshotValidationError,
    HistoricalMatch,
    MatchScheduleRevision,
    MetricScope,
    PlayerStatLine,
    ResultRevision,
    RosterRevision,
    SetScore,
    StatLine,
    TargetMatch,
    compute_snapshot_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = REPO_ROOT / "contracts" / "feature-v1.schema.json"
START = datetime(2030, 1, 10, 12, tzinfo=UTC)
CUTOFF = START - timedelta(minutes=60)


def _target(*, schedule_observed_at: datetime | None = None) -> TargetMatch:
    return TargetMatch(
        id="target",
        schedule_revision_id="schedule-1",
        season_id="season-1",
        competition_id="regular",
        scheduled_start_at=START,
        schedule_observed_at=schedule_observed_at or CUTOFF - timedelta(days=1),
        home_team_id="H",
        away_team_id="A",
    )


def _sets(home_wins: int, away_wins: int) -> tuple[SetScore, ...]:
    return tuple(
        [SetScore(25, 20) for _ in range(home_wins)] + [SetScore(20, 25) for _ in range(away_wins)]
    )


def _verified_line(**changes: float) -> StatLine:
    values = {
        "attack_kills": 10.0,
        "attack_errors": 1.0,
        "attack_attempts": 20.0,
        "serve_aces": 2.0,
        "serve_attempts": 20.0,
        "block_points": 3.0,
        "receive_successes": 15.0,
        "receive_attempts": 20.0,
        "errors": 4.0,
    }
    values.update(changes)
    return StatLine(VERIFIED_METRIC_SCHEMA_VERSION, values)


def _match(
    number: int,
    *,
    home: str,
    away: str,
    home_sets: int,
    away_sets: int,
    ended_at: datetime | None = None,
    observed_at: datetime | None = None,
    revisions: tuple[ResultRevision, ...] | None = None,
    stat_line: StatLine | None = None,
    player_attempts: float | None = None,
    player_team: str | None = None,
    roster_team: str | None = None,
    include_player_stat: bool = True,
) -> HistoricalMatch:
    end = ended_at or CUTOFF - timedelta(days=number)
    observed = observed_at or end + timedelta(minutes=1)
    line = stat_line or _verified_line()
    represented_team = player_team or ("H" if "H" in {home, away} else home)
    confirmed_team = roster_team or represented_team
    result = ResultRevision(
        id=f"result-{number}-1",
        revision=1,
        observed_at=observed,
        raw_snapshot_sha256=f"{number % 10}" * 64,
        home_sets=home_sets,
        away_sets=away_sets,
        sets=_sets(home_sets, away_sets),
        team_stats={home: line, away: line},
        player_stats=(
            {
                "P": PlayerStatLine(
                    represented_team,
                    StatLine(
                        line.metric_schema_version,
                        {"attack_attempts": player_attempts},
                        line.scope,
                    ),
                )
            }
            if player_attempts is not None and include_player_stat
            else {}
        ),
        roster_player_ids=({confirmed_team: ("P",)} if player_attempts is not None else {}),
    )
    schedule = MatchScheduleRevision(
        id=f"schedule-{number}-1",
        revision=1,
        observed_at=end - timedelta(days=1),
        raw_snapshot_sha256=f"{(number + 6) % 10}" * 64,
        scheduled_start_at=end - timedelta(hours=2),
        ended_at=end,
        home_team_id=home,
        away_team_id=away,
    )
    return HistoricalMatch(
        id=f"match-{number}",
        season_id="season-1",
        competition_id="regular",
        schedule_revisions=(schedule,),
        result_revisions=revisions or (result,),
    )


def _build(
    matches: list[HistoricalMatch],
    *,
    policy: AvailabilityPolicy = AvailabilityPolicy.HISTORICAL_POINT_IN_TIME,
    rosters: list[RosterRevision] | None = None,
    target: TargetMatch | None = None,
):
    return AsOfFeatureBuilder().build(
        target=target or _target(),
        cutoff_at=CUTOFF,
        captured_at=CUTOFF + timedelta(seconds=1),
        policy=policy,
        matches=matches,
        roster_revisions=rosters or [],
    )


def test_cutoff_boundaries_and_corrections_select_policy_specific_revision() -> None:
    before = CUTOFF - timedelta(microseconds=1)
    original = ResultRevision(
        "original",
        1,
        before,
        "a" * 64,
        3,
        0,
        _sets(3, 0),
    )
    correction_at_cutoff = ResultRevision(
        "correction-at-cutoff",
        2,
        CUTOFF,
        "b" * 64,
        3,
        1,
        _sets(3, 1),
    )
    correction_after = ResultRevision(
        "correction-after",
        3,
        CUTOFF + timedelta(microseconds=1),
        "c" * 64,
        0,
        3,
        _sets(0, 3),
    )
    eligible = _match(
        1,
        home="H",
        away="X",
        home_sets=3,
        away_sets=0,
        ended_at=before,
        revisions=(original, correction_at_cutoff, correction_after),
    )
    ends_at_cutoff = _match(
        2,
        home="H",
        away="X",
        home_sets=0,
        away_sets=3,
        ended_at=CUTOFF,
        observed_at=CUTOFF,
    )

    strict = _build([eligible, ends_at_cutoff])
    reconstructed = _build(
        [eligible, ends_at_cutoff],
        policy=AvailabilityPolicy.HISTORICAL_RECONSTRUCTION,
    )

    strict_season = strict.team_features["home"]["season_win_rate"]
    reconstructed_season = reconstructed.team_features["home"]["season_win_rate"]
    assert strict_season.value == 1.0
    assert strict_season.lineage.result_revision_ids == ("correction-at-cutoff",)
    assert reconstructed_season.value == 0.0
    assert reconstructed_season.lineage.result_revision_ids == ("correction-after",)
    assert "match-2" not in strict.lineage.included_match_ids


def test_live_and_point_in_time_reject_schedule_first_seen_after_cutoff() -> None:
    late_target = _target(schedule_observed_at=CUTOFF + timedelta(microseconds=1))

    for policy in (
        AvailabilityPolicy.HISTORICAL_POINT_IN_TIME,
        AvailabilityPolicy.LIVE_PROSPECTIVE,
    ):
        with pytest.raises(ValueError, match="target schedule by cutoff"):
            _build([], policy=policy, target=late_target)

    snapshot = _build(
        [],
        policy=AvailabilityPolicy.HISTORICAL_RECONSTRUCTION,
        target=late_target,
    )
    assert snapshot.availability_policy is AvailabilityPolicy.HISTORICAL_RECONSTRUCTION


def test_historical_schedule_revision_is_selected_as_of_and_recorded_in_lineage() -> None:
    match = _match(1, home="H", away="X", home_sets=3, away_sets=0)
    original = match.schedule_revisions[0]
    corrected = replace(
        original,
        id="schedule-corrected-after-cutoff",
        revision=2,
        observed_at=CUTOFF + timedelta(microseconds=1),
        raw_snapshot_sha256="e" * 64,
        ended_at=CUTOFF + timedelta(minutes=1),
    )
    revised_match = replace(match, schedule_revisions=(original, corrected))

    strict = _build([revised_match])
    reconstructed = _build(
        [revised_match],
        policy=AvailabilityPolicy.HISTORICAL_RECONSTRUCTION,
    )

    strict_season = strict.team_features["home"]["season_win_rate"]
    assert strict_season.value == 1.0
    assert strict_season.lineage.schedule_revision_ids == (original.id,)
    assert original.raw_snapshot_sha256 in strict_season.lineage.raw_snapshot_sha256
    assert corrected.raw_snapshot_sha256 not in strict_season.lineage.raw_snapshot_sha256
    assert reconstructed.team_features["home"]["season_win_rate"].value is None


def test_hand_calculated_match_set_split_h2h_and_verified_metrics() -> None:
    matches = [
        _match(1, home="H", away="A", home_sets=3, away_sets=0, player_attempts=10),
        _match(2, home="A", away="H", home_sets=3, away_sets=0, player_attempts=9),
        _match(3, home="H", away="X", home_sets=3, away_sets=1, player_attempts=8),
        _match(4, home="X", away="H", home_sets=1, away_sets=3, player_attempts=3),
        _match(5, home="H", away="A", home_sets=2, away_sets=3, player_attempts=2),
        _match(6, home="X", away="H", home_sets=0, away_sets=3, player_attempts=1),
    ]
    rosters = [
        RosterRevision("roster-p", "target", "H", 1, CUTOFF, "d" * 64, ("P",), True),
        RosterRevision("roster-q", "target", "A", 1, CUTOFF, "e" * 64, ("Q",), True),
    ]

    snapshot = _build(matches, rosters=rosters)
    home = snapshot.team_features["home"]

    assert home["recent_5_match_win_rate"].value == pytest.approx(3 / 5)
    assert home["recent_10_match_win_rate"].value == pytest.approx(4 / 6)
    assert home["recent_10_set_win_rate"].value == pytest.approx(6 / 10)
    assert home["season_win_rate"].value == pytest.approx(4 / 6)
    assert home["venue_split_win_rate"].value == pytest.approx(2 / 3)
    assert snapshot.matchup_features["head_to_head_home_win_rate"].value == pytest.approx(1 / 3)

    assert home["attack_efficiency"].value == pytest.approx((60 - 6) / 120)
    assert home["serve_ace_rate"].value == pytest.approx(12 / 120)
    assert home["block_points_per_set"].value == pytest.approx(18 / 22)
    assert home["receive_efficiency"].value == pytest.approx(90 / 120)
    assert home["errors_per_set"].value == pytest.approx(24 / 22)

    player = snapshot.players["P"].features
    assert player["attack_share_recent_5"].value == pytest.approx(32 / 100)
    assert player["attack_share_trend_3v3"].value == pytest.approx(0.45 - 0.1)
    assert player["attack_share_recent_5"].lineage.roster_revision_ids == ("roster-p",)
    assert snapshot.lineup_status.value == "known"


def test_unverified_cumulative_zero_denominator_and_missing_stay_unknown() -> None:
    raw = StatLine(
        "kovo-observed-match-metrics-v1",
        {"attack_kills": 0, "attack_errors": 0, "attack_attempts": 0},
    )
    raw_snapshot = _build([_match(1, home="H", away="X", home_sets=3, away_sets=0, stat_line=raw)])
    raw_attack = raw_snapshot.team_features["home"]["attack_efficiency"]
    assert raw_attack.status.value == "unknown"
    assert raw_attack.value is None
    assert raw_attack.missing_reason.value == "source_metric_unverified"

    cumulative = StatLine(
        VERIFIED_METRIC_SCHEMA_VERSION,
        _verified_line().values,
        MetricScope.CUMULATIVE,
    )
    cumulative_snapshot = _build(
        [_match(1, home="H", away="X", home_sets=3, away_sets=0, stat_line=cumulative)]
    )
    assert (
        cumulative_snapshot.team_features["home"]["attack_efficiency"].missing_reason.value
        == "cumulative_value_rejected"
    )

    zero = _verified_line(attack_kills=0, attack_errors=0, attack_attempts=0)
    zero_snapshot = _build(
        [_match(1, home="H", away="X", home_sets=3, away_sets=0, stat_line=zero)]
    )
    zero_attack = zero_snapshot.team_features["home"]["attack_efficiency"]
    assert zero_attack.value is None
    assert zero_attack.missing_reason.value == "zero_denominator"

    missing_snapshot = _build([])
    assert missing_snapshot.team_features["home"]["season_win_rate"].value is None
    assert (
        missing_snapshot.team_features["home"]["season_win_rate"].missing_reason.value
        == "no_prior_matches"
    )


def test_short_season_is_explicitly_insufficient_instead_of_zero() -> None:
    snapshot = _build(
        [
            _match(1, home="H", away="X", home_sets=3, away_sets=0),
            _match(2, home="X", away="H", home_sets=3, away_sets=1),
        ]
    )

    recent = snapshot.team_features["home"]["recent_5_match_win_rate"]
    season = snapshot.team_features["home"]["season_win_rate"]
    assert recent.value is None
    assert recent.sample_size == 2
    assert recent.missing_reason.value == "insufficient_sample"
    assert season.value == pytest.approx(0.5)


def test_player_history_never_combines_stats_from_a_previous_team() -> None:
    matches = [
        _match(1, home="H", away="X", home_sets=3, away_sets=0, player_attempts=10),
        _match(2, home="X", away="H", home_sets=0, away_sets=3, player_attempts=8),
        _match(
            3,
            home="H",
            away="X",
            home_sets=3,
            away_sets=1,
            player_attempts=20,
            player_team="X",
            roster_team="X",
        ),
    ]
    roster = RosterRevision("target-home", "target", "H", 1, CUTOFF, "a" * 64, ("P",), True)

    snapshot = _build(matches, rosters=[roster])
    share = snapshot.players["P"].features["attack_share_recent_5"]

    assert share.value == pytest.approx(18 / 40)
    assert share.lineage.included_match_ids == ("match-1", "match-2")
    assert "match-3" not in share.lineage.included_match_ids


def test_player_window_keeps_missing_latest_appearance_and_does_not_backfill() -> None:
    matches = [
        _match(
            number,
            home="H" if number % 2 else "X",
            away="X" if number % 2 else "H",
            home_sets=3 if number % 2 else 0,
            away_sets=0 if number % 2 else 3,
            player_attempts=float(11 - number),
            include_player_stat=number != 1,
        )
        for number in range(1, 7)
    ]
    roster = RosterRevision("target-home", "target", "H", 1, CUTOFF, "a" * 64, ("P",), True)

    snapshot = _build(matches, rosters=[roster])
    share = snapshot.players["P"].features["attack_share_recent_5"]

    assert share.value is None
    assert share.missing_reason.value == "missing_input"
    assert share.lineage.included_match_ids == (
        "match-1",
        "match-2",
        "match-3",
        "match-4",
        "match-5",
    )
    assert "match-6" not in share.lineage.included_match_ids


def test_target_match_roster_is_never_injected_from_after_cutoff() -> None:
    after = RosterRevision(
        "post-match-player",
        "target",
        "H",
        2,
        CUTOFF + timedelta(microseconds=1),
        "f" * 64,
        ("P",),
        True,
    )
    for policy in AvailabilityPolicy:
        snapshot = _build([], policy=policy, rosters=[after])
        assert snapshot.lineup_status.value == "unknown"
        assert snapshot.players == {}
        assert snapshot.lineage.roster_revision_ids == ()

    before = RosterRevision("pregame-player", "target", "H", 1, CUTOFF, "a" * 64, ("P",), False)
    snapshot = _build([], rosters=[before, after])
    assert snapshot.lineup_status.value == "partial"
    assert set(snapshot.players) == {"P"}
    assert (
        snapshot.players["P"].features["attack_share_recent_5"].missing_reason.value
        == "missing_input"
    )


def test_latest_roster_batches_are_atomic_and_do_not_merge_stale_players() -> None:
    rosters = [
        RosterRevision(
            "home-old",
            "target",
            "H",
            1,
            CUTOFF - timedelta(minutes=2),
            "1" * 64,
            ("OLD", "P"),
            True,
        ),
        RosterRevision("home-latest", "target", "H", 2, CUTOFF, "2" * 64, ("P2",), False),
        RosterRevision("away-latest", "target", "A", 1, CUTOFF, "3" * 64, ("Q",), True),
    ]

    snapshot = _build([], rosters=rosters)

    assert snapshot.lineup_status.value == "partial"
    assert set(snapshot.players) == {"P2", "Q"}
    assert snapshot.lineage.roster_revision_ids == ("away-latest", "home-latest")
    assert "OLD" not in snapshot.players


class _ScalarResult:
    def scalar_one(self) -> UUID:
        return UUID(int=1)


class _ConnectionSpy:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *_args: object, **_kwargs: object) -> _ScalarResult:
        self.calls += 1
        return _ScalarResult()


def test_snapshot_nested_values_are_defensively_copied_and_repository_revalidates() -> None:
    snapshot = _build([])
    source = {side: dict(features) for side, features in snapshot.team_features.items()}
    copied = replace(snapshot, team_features=source)
    source["home"].clear()
    assert copied.team_features["home"]
    with pytest.raises(TypeError):
        copied.team_features["home"]["mutated"] = next(  # type: ignore[index]
            iter(copied.team_features["home"].values())
        )

    connection = _ConnectionSpy()
    repository = FeatureSnapshotRepository(connection, CONTRACT)  # type: ignore[arg-type]
    bad_hash = replace(snapshot, sha256="0" * 64)
    with pytest.raises(FeatureSnapshotValidationError, match="SHA-256"):
        repository.add(bad_hash)
    assert connection.calls == 0

    invalid_base = replace(snapshot, feature_version="invalid", sha256="")
    invalid = replace(invalid_base, sha256=compute_snapshot_sha256(invalid_base))
    with pytest.raises(FeatureSnapshotValidationError, match="schema validation"):
        repository.add(invalid)
    assert connection.calls == 0

    assert repository.add(snapshot) == UUID(int=1)
    assert connection.calls == 1


def test_snapshot_and_definition_catalog_match_feature_v1_schema() -> None:
    schema: dict[str, Any] = json.loads(CONTRACT.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    snapshot = _build([]).to_dict()

    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        ).iter_errors(snapshot),
        key=lambda error: list(error.path),
    )
    assert errors == []

    schema_definitions = schema["x-vlytics-feature-definitions"]
    assert [item["key"] for item in schema_definitions] == [
        item.key for item in FEATURE_DEFINITIONS
    ]
    for schema_definition, code_definition in zip(
        schema_definitions, FEATURE_DEFINITIONS, strict=True
    ):
        assert schema_definition["formula"] == code_definition.formula
        assert schema_definition["denominator"] == code_definition.denominator
        assert schema_definition["window"] == code_definition.window
        assert schema_definition["min_samples"] == code_definition.min_samples
        assert schema_definition["missing_reasons"] == [
            reason.value for reason in code_definition.missing_reasons
        ]
