"""Hand-calculated tests for point-in-time Elo baseline experiments."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import log

import pytest

from vlytics.engine.experiments import (
    LOG_LOSS_EPSILON,
    HomeWinRateBaseline,
    TemporalSplit,
    default_candidate_grid,
    evaluate_probabilities,
    run_baseline_experiment,
    run_baseline_suite,
    walk_forward_probabilities,
)
from vlytics.engine.features import AvailabilityPolicy
from vlytics.engine.predictors import (
    Division,
    EloConfig,
    EloMatch,
    EloPredictor,
    EloResultRevision,
    FranchiseIdentity,
    RatingSource,
    ResultFinality,
    ResultFinalityPolicy,
    TimingEligibility,
    reproduction_candidate,
)

START = datetime(2030, 1, 1, tzinfo=UTC)
POLICY = AvailabilityPolicy.HISTORICAL_POINT_IN_TIME


def _result(
    day: int,
    *,
    match_id: str | None = None,
    score: tuple[int, int] = (3, 0),
    revision: int = 1,
    observed_at: datetime | None = None,
    finalized_at: datetime | None = None,
    finality: ResultFinality = ResultFinality.FINAL,
) -> EloResultRevision:
    final_time = finalized_at or START + timedelta(days=day, hours=3)
    return EloResultRevision(
        match_id=match_id or f"match-{day:02d}",
        revision_id=f"result-{day:02d}-{revision}",
        revision=revision,
        observed_at=observed_at or final_time + timedelta(minutes=1),
        finalized_at=final_time,
        raw_snapshot_sha256=f"{revision % 10}" * 64,
        finality=finality,
        home_sets=score[0],
        away_sets=score[1],
    )


def _match(
    day: int,
    *,
    match_id: str | None = None,
    season: str = "s1",
    division: Division = Division.MEN,
    home: str = "H",
    away: str = "A",
    score: tuple[int, int] | None = (3, 0),
    result_revisions: tuple[EloResultRevision, ...] | None = None,
    home_franchise: FranchiseIdentity | None = None,
    away_franchise: FranchiseIdentity | None = None,
    policy: AvailabilityPolicy = POLICY,
    timing: TimingEligibility | None = None,
    competition: str = "regular",
    stage: str = "regular_season",
    mapping_version: str = "franchise-v1",
    input_version: str = "elo-match-v2",
    scheduled_start_at: datetime | None = None,
    schedule_observed_at: datetime | None = None,
) -> EloMatch:
    resolved_match_id = match_id or f"match-{day:02d}"
    cutoff = START + timedelta(days=day)
    scheduled = scheduled_start_at or cutoff + timedelta(hours=1)
    if result_revisions is None:
        result_revisions = (
            (
                _result(
                    day,
                    match_id=resolved_match_id,
                    score=score,
                    finalized_at=scheduled + timedelta(hours=2),
                ),
            )
            if score
            else ()
        )
    resolved_timing = timing or (
        TimingEligibility.RECONSTRUCTED
        if policy is AvailabilityPolicy.HISTORICAL_RECONSTRUCTION
        else TimingEligibility.ON_TIME
    )
    return EloMatch(
        match_id=resolved_match_id,
        schedule_revision_id=f"schedule-{resolved_match_id}",
        schedule_revision=1,
        schedule_observed_at=schedule_observed_at or cutoff - timedelta(days=1),
        schedule_raw_snapshot_sha256=f"{(day + 5) % 10}" * 64,
        prediction_cutoff_at=cutoff,
        scheduled_start_at=scheduled,
        season_id=season,
        competition=competition,
        stage=stage,
        division=division,
        home_team_id=home,
        away_team_id=away,
        availability_policy=policy,
        timing_eligibility=resolved_timing,
        result_finality_policy=ResultFinalityPolicy.FINAL_ONLY,
        franchise_mapping_version=mapping_version,
        input_version=input_version,
        result_revisions=result_revisions,
        home_franchise=home_franchise,
        away_franchise=away_franchise,
    )


def _split() -> TemporalSplit:
    return TemporalSplit(
        train_start=START,
        train_end=START + timedelta(days=2),
        validation_start=START + timedelta(days=2),
        validation_end=START + timedelta(days=4),
        test_start=START + timedelta(days=4),
        test_end=START + timedelta(days=6),
    )


def test_predict_is_pure_and_transition_and_result_update_are_idempotent() -> None:
    config = EloConfig(k_factor=32, home_advantage=0, season_regression=0.5)
    predictor = EloPredictor(Division.MEN, config)
    first_match = _match(0)

    with pytest.raises(ValueError, match="transition"):
        predictor.predict(first_match)
    assert predictor.transition(first_match) is True
    ratings_before = dict(predictor.ratings)
    first = predictor.predict(first_match)
    assert predictor.predict(first_match) == first
    assert dict(predictor.ratings) == ratings_before
    assert predictor.transition(first_match) is False

    result = first_match.select_result_as_of(START + timedelta(days=1))
    assert result is not None
    assert (
        predictor.update_observed_result(
            first_match,
            result,
            known_at=START + timedelta(days=1),
        )
        is True
    )
    assert (
        predictor.update_observed_result(
            first_match,
            result,
            known_at=START + timedelta(days=1),
        )
        is False
    )

    second_match = _match(1, score=(0, 3))
    predictor.transition(second_match)
    second = predictor.predict(second_match)
    # 3:0 weight is 1.25, so 32 * 1.25 * (1 - .5) = 20.
    assert first.home_win_probability == 0.5
    assert second.home_rating == 1520
    assert second.away_rating == 1480
    assert second.home_win_probability == pytest.approx(1 / (1 + 10 ** (-40 / 400)))
    with pytest.raises(ValueError, match="strict cutoff order"):
        predictor.transition(first_match)


def test_observed_result_can_be_appended_after_the_pure_prediction() -> None:
    predictor = EloPredictor(Division.MEN, EloConfig(32, 0, 0.5))
    scheduled = _match(0, score=None)
    predictor.transition(scheduled)
    prediction = predictor.predict(scheduled)
    observed = replace(
        scheduled,
        result_revisions=(_result(0, match_id=scheduled.match_id),),
    )
    result = observed.select_result_as_of(START + timedelta(days=1))

    assert result is not None
    assert observed.prediction_fingerprint == scheduled.prediction_fingerprint
    assert observed.fingerprint != scheduled.fingerprint
    assert predictor.update_observed_result(
        observed,
        result,
        known_at=START + timedelta(days=1),
    )
    assert prediction.home_win_probability == 0.5
    assert predictor.ratings["H"] == 1520


def test_season_regression_uses_only_verified_franchise_carryover() -> None:
    identity_h = FranchiseIdentity("fh", "franchise-v1", "reviewed source records")
    identity_a = FranchiseIdentity("fa", "franchise-v1", "reviewed source records")
    predictor = EloPredictor(
        Division.MEN,
        EloConfig(k_factor=32, home_advantage=0, season_regression=0.5),
    )
    first = _match(0, home_franchise=identity_h, away_franchise=identity_a)
    predictor.transition(first)
    result = first.select_result_as_of(START + timedelta(days=1))
    assert result is not None
    predictor.update_observed_result(first, result, known_at=START + timedelta(days=1))

    next_season = _match(
        1,
        season="s2",
        home="H-new-code",
        away="A-new-code",
        score=None,
        home_franchise=identity_h,
        away_franchise=identity_a,
    )
    predictor.transition(next_season)
    carried = predictor.predict(next_season)

    assert carried.home_rating == 1510
    assert carried.away_rating == 1490
    assert carried.home_rating_source is RatingSource.VERIFIED_FRANCHISE_CARRYOVER
    assert carried.home_franchise_id == "fh"
    assert carried.franchise_mapping_version == "franchise-v1"

    isolated = EloPredictor(
        Division.MEN,
        EloConfig(k_factor=32, home_advantage=0, season_regression=0.5),
    )
    unverified_first = _match(0)
    isolated.transition(unverified_first)
    unverified_result = unverified_first.select_result_as_of(START + timedelta(days=1))
    assert unverified_result is not None
    isolated.update_observed_result(
        unverified_first,
        unverified_result,
        known_at=START + timedelta(days=1),
    )
    unverified_next = _match(
        1,
        season="s2",
        home="H-new-code",
        away="A-new-code",
        score=None,
    )
    isolated.transition(unverified_next)
    assert isolated.predict(unverified_next).home_rating == 1500


def test_result_selection_separates_strict_and_reconstruction_corrections() -> None:
    match_id = "corrected"
    finalized = START + timedelta(hours=3)
    original = _result(
        0,
        match_id=match_id,
        score=(3, 0),
        finalized_at=finalized,
        observed_at=finalized + timedelta(minutes=1),
    )
    correction = _result(
        0,
        match_id=match_id,
        score=(0, 3),
        revision=2,
        finalized_at=finalized,
        observed_at=START + timedelta(days=3),
    )
    strict = _match(0, match_id=match_id, result_revisions=(original, correction))
    reconstructed = replace(
        strict,
        availability_policy=AvailabilityPolicy.HISTORICAL_RECONSTRUCTION,
        timing_eligibility=TimingEligibility.RECONSTRUCTED,
        schedule_observed_at=START + timedelta(days=2),
    )

    assert strict.select_result_as_of(START + timedelta(days=2)) == original
    assert strict.select_result_as_of(START + timedelta(days=4)) == correction
    assert reconstructed.select_result_as_of(START + timedelta(days=2)) == correction

    void = _result(
        0,
        match_id=match_id,
        revision=3,
        finality=ResultFinality.VOID,
        finalized_at=finalized,
        observed_at=START + timedelta(days=5),
    )
    voided = replace(strict, result_revisions=(original, correction, void))
    assert voided.select_result_as_of(START + timedelta(days=6)) is None
    assert voided.evaluation_result is None


def test_walk_forward_delays_postponed_results_and_replays_corrections() -> None:
    match_id = "corrected"
    first = _result(0, match_id=match_id, score=(3, 0))
    correction = _result(
        0,
        match_id=match_id,
        score=(0, 3),
        revision=2,
        finalized_at=first.finalized_at,
        observed_at=START + timedelta(days=3),
    )
    corrected = _match(0, match_id=match_id, result_revisions=(first, correction))
    matches = [
        corrected,
        _match(1, match_id="target-1", score=None),
        _match(2, match_id="target-2", score=None),
        _match(4, match_id="target-4", score=None),
    ]
    probabilities = walk_forward_probabilities(
        matches,
        EloConfig(32, 0, 0.5),
    )

    assert probabilities["target-1"] > 0.5
    assert probabilities["target-2"] > 0.5
    assert probabilities["target-4"] < 0.5

    postponed = _match(
        0,
        match_id="postponed",
        scheduled_start_at=START + timedelta(days=3, hours=1),
        result_revisions=(
            _result(
                0,
                match_id="postponed",
                finalized_at=START + timedelta(days=3, hours=3),
            ),
        ),
    )
    postponed_probabilities = walk_forward_probabilities(
        [
            postponed,
            _match(1, match_id="before-1", score=None),
            _match(2, match_id="before-2", score=None),
            _match(4, match_id="after", score=None),
        ],
        EloConfig(32, 0, 0.5),
    )
    assert postponed_probabilities["before-1"] == 0.5
    assert postponed_probabilities["before-2"] == 0.5
    assert postponed_probabilities["after"] > 0.5


def test_home_rate_uses_only_results_known_at_training_boundary() -> None:
    baseline = HomeWinRateBaseline()
    late = _result(
        1,
        observed_at=START + timedelta(days=3),
    )
    train = [
        _match(0, score=(3, 0)),
        _match(1, result_revisions=(late,)),
    ]
    baseline.fit(train, known_at=START + timedelta(days=2))

    assert baseline.sample_size == 1
    assert baseline.home_wins == 1
    assert baseline.probability == 1.0
    assert baseline.result_revision_ids == ("result-00-1",)


def test_metrics_use_architecture_log_loss_epsilon_and_report_boundaries() -> None:
    matches = [_match(0, score=(3, 0)), _match(1, score=(0, 3))]
    report = evaluate_probabilities(matches, [0.0, 1.0])

    assert LOG_LOSS_EPSILON == 1e-12
    assert report.brier_score == 1.0
    assert report.log_loss == pytest.approx(-log(LOG_LOSS_EPSILON))
    assert report.accuracy == 0.0
    assert report.coverage == 1.0
    assert report.result_revision_ids == ("result-00-1", "result-01-1")
    assert report.result_raw_snapshot_sha256 == ("1" * 64, "1" * 64)


def test_mixed_stage_competition_timing_and_versions_are_rejected() -> None:
    base = _match(0)
    variants = [
        _match(1, competition="playoff"),
        _match(1, stage="championship"),
        _match(1, mapping_version="franchise-v2"),
        _match(1, input_version="elo-match-v3"),
        _match(
            1,
            policy=AvailabilityPolicy.HISTORICAL_RECONSTRUCTION,
            timing=TimingEligibility.RECONSTRUCTED,
        ),
    ]
    for variant in variants:
        with pytest.raises(ValueError, match="cohort dimensions"):
            evaluate_probabilities([base, variant], [0.5, 0.5])


def test_validation_selection_is_frozen_and_manifest_is_canonical() -> None:
    matches = [_match(day, home="A", away="B", score=(3, 0)) for day in range(6)]
    candidates = (
        EloConfig(8, 0, 0.5, model_version="candidate-v1", random_seed=7),
        EloConfig(48, 0, 0.5, model_version="candidate-v1", random_seed=7),
        reproduction_candidate(),
    )
    original = run_baseline_experiment(matches, _split(), candidates)
    reversed_test = [
        replace(
            match,
            result_revisions=(
                _result(
                    day,
                    match_id=match.match_id,
                    score=(0, 3),
                ),
            ),
        )
        if day >= 4
        else match
        for day, match in enumerate(matches)
    ]
    changed = run_baseline_experiment(reversed_test, _split(), candidates)
    shuffled = run_baseline_experiment(list(reversed(matches)), _split(), candidates)

    assert original.selected_config.config_id == changed.selected_config.config_id
    assert original.selected_config.k_factor == 48
    assert original.test_parameters_frozen is True
    assert original.selection_data_ends_before == _split().test_start
    assert original.elo_test.brier_score != changed.elo_test.brier_score
    assert original.input_manifest.sha256 == shuffled.input_manifest.sha256
    document = original.to_dict()
    assert document["split"] == _split().to_dict()
    first_entry = document["input_manifest"]["entries"][0]
    assert first_entry["schedule_revision_id"] == "schedule-match-00"
    assert first_entry["schedule_raw_snapshot_sha256"] == "5" * 64
    assert first_entry["result_revisions"][0]["revision_id"] == "result-00-1"
    assert first_entry["result_revisions"][0]["raw_snapshot_sha256"] == "1" * 64


def test_suite_keeps_division_and_availability_cohorts_separate() -> None:
    men = [_match(day) for day in range(6)]
    women = [
        _match(day, match_id=f"women-{day}", division=Division.WOMEN, score=(0, 3))
        for day in range(6)
    ]
    reconstruction = [
        replace(
            match,
            match_id=f"reconstructed-{match.match_id}",
            schedule_revision_id=f"reconstructed-{match.schedule_revision_id}",
            availability_policy=AvailabilityPolicy.HISTORICAL_RECONSTRUCTION,
            timing_eligibility=TimingEligibility.RECONSTRUCTED,
            result_revisions=tuple(
                replace(
                    result,
                    match_id=f"reconstructed-{result.match_id}",
                    revision_id=f"reconstructed-{result.revision_id}",
                )
                for result in match.result_revisions
            ),
        )
        for match in men
    ]
    suite = run_baseline_suite(
        men + women + reconstruction,
        _split(),
        candidates=(reproduction_candidate(),),
    )

    assert len(suite.reports) == 3
    assert {
        (report.cohort.division, report.cohort.availability_policy) for report in suite.reports
    } == {
        (Division.MEN, AvailabilityPolicy.HISTORICAL_POINT_IN_TIME),
        (Division.MEN, AvailabilityPolicy.HISTORICAL_RECONSTRUCTION),
        (Division.WOMEN, AvailabilityPolicy.HISTORICAL_POINT_IN_TIME),
    }


def test_candidate_identity_and_input_validation_are_explicit() -> None:
    candidate = reproduction_candidate()
    assert candidate.config_id == EloConfig(32, 15, 0.5).config_id
    assert candidate.model_version == "elo-home-win-v2"
    assert candidate.random_seed == 0
    assert len(default_candidate_grid()) == 60
    assert candidate.config_id in {config.config_id for config in default_candidate_grid()}

    with pytest.raises(ValueError, match="observed by cutoff"):
        _match(0, schedule_observed_at=START + timedelta(minutes=1))
    with pytest.raises(ValueError, match="three-set winner"):
        _result(0, score=(2, 1))
