"""Math, lineage, contract, and holdout tests for set outcomes."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vlytics.engine.experiments.baselines import TemporalSplit, walk_forward_predictions
from vlytics.engine.experiments.sets import (
    CALIBRATION_INTERVAL_VERSION,
    EXACT_SCORE_TIE_POLICY_VERSION,
    SET_METRIC_VERSION,
    evaluate_set_predictions,
    run_set_holdout,
    run_set_holdout_suite,
)
from vlytics.engine.features import AvailabilityPolicy
from vlytics.engine.prediction_validation import (
    PredictionContractError,
    PredictionValidationContext,
    validate_prediction_output_v1,
)
from vlytics.engine.predictors.elo import (
    Division,
    EloConfig,
    EloMatch,
    EloPrediction,
    EloResultRevision,
    ResultFinality,
    ResultFinalityPolicy,
    TimingEligibility,
)
from vlytics.engine.predictors.sets import (
    SET_OUTCOME_ORDER,
    PredictionCapability,
    SetModelConfig,
    SetOutcome,
    SetOutcomePredictor,
    independent_set_distribution,
    solve_regular_set_probability,
)

START = datetime(2030, 1, 1, tzinfo=UTC)
ELO_CONFIG = EloConfig(k_factor=32, home_advantage=0, season_regression=0.5)


def _match(
    day: int,
    *,
    match_id: str | None = None,
    division: Division = Division.MEN,
    score: tuple[int, int] | None = (3, 0),
    stage: str = "regular",
) -> EloMatch:
    selected_match_id = match_id or f"match-{day:02d}"
    scheduled_start = START + timedelta(days=day)
    result_revisions = ()
    if score is not None:
        result_revisions = (
            EloResultRevision(
                match_id=selected_match_id,
                revision_id=f"result-{selected_match_id}",
                revision=1,
                observed_at=scheduled_start + timedelta(hours=3),
                finalized_at=scheduled_start + timedelta(hours=2),
                raw_snapshot_sha256="b" * 64,
                finality=ResultFinality.FINAL,
                home_sets=score[0],
                away_sets=score[1],
            ),
        )
    return EloMatch(
        match_id=selected_match_id,
        schedule_revision_id=f"schedule-{selected_match_id}",
        schedule_revision=1,
        schedule_observed_at=scheduled_start - timedelta(hours=2),
        schedule_raw_snapshot_sha256="a" * 64,
        prediction_cutoff_at=scheduled_start - timedelta(hours=1),
        scheduled_start_at=scheduled_start,
        season_id="s1",
        competition="regular",
        stage=stage,
        division=division,
        home_team_id="H",
        away_team_id="A",
        availability_policy=AvailabilityPolicy.HISTORICAL_RECONSTRUCTION,
        timing_eligibility=TimingEligibility.RECONSTRUCTED,
        result_finality_policy=ResultFinalityPolicy.FINAL_ONLY,
        franchise_mapping_version="mapping-v1",
        result_revisions=result_revisions,
    )


def _upstream(timeline: list[EloMatch], target: EloMatch) -> EloPrediction:
    return walk_forward_predictions(timeline + [target], ELO_CONFIG)[target.match_id]


def _schema() -> dict[str, object]:
    path = Path(__file__).parents[3] / "contracts" / "prediction-v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("p", "p5", "expected"),
    [
        (0.0, 0.0, SetOutcome.AWAY_3_0),
        (0.0, 1.0, SetOutcome.AWAY_3_0),
        (1.0, 0.0, SetOutcome.HOME_3_0),
        (1.0, 1.0, SetOutcome.HOME_3_0),
    ],
)
def test_probability_boundaries_are_valid(p: float, p5: float, expected: SetOutcome) -> None:
    distribution = independent_set_distribution(p, p5)
    assert tuple(item.outcome for item in distribution.outcomes) == SET_OUTCOME_ORDER
    assert distribution[expected] == 1.0
    assert sum(distribution.probabilities) == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf")])
def test_invalid_probability_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="between zero and one"):
        independent_set_distribution(value, 0.5)
    with pytest.raises(ValueError, match="between zero and one"):
        solve_regular_set_probability(0.5, value)


def test_distribution_has_mass_one_and_home_away_symmetry() -> None:
    home = independent_set_distribution(0.63, 0.57)
    swapped = independent_set_distribution(0.37, 0.43)
    assert sum(home.probabilities) == pytest.approx(1.0, abs=1e-12)
    assert home.probabilities == pytest.approx(tuple(reversed(swapped.probabilities)))


@pytest.mark.parametrize("winner_probability", [0.0, 0.01, 0.25, 0.5, 0.73, 0.99, 1.0])
@pytest.mark.parametrize("p5", [0.0, 0.35, 0.5, 0.8, 1.0])
def test_regular_set_probability_preserves_winner_marginal(
    winner_probability: float,
    p5: float,
) -> None:
    p = solve_regular_set_probability(winner_probability, p5)
    distribution = independent_set_distribution(p, p5)
    assert 0 <= p <= 1
    assert distribution.home_win_probability == pytest.approx(winner_probability, abs=1e-12)


def test_fitted_model_persists_full_cohort_manifest_and_upstream_lineage() -> None:
    config = SetModelConfig(random_seed=17)
    training = [_match(0, score=(3, 2)), _match(1, score=(2, 3)), _match(2, score=(3, 1))]
    known_at = START + timedelta(days=3, hours=-1)
    first = SetOutcomePredictor.fit(training, config, known_at=known_at)
    second = SetOutcomePredictor.fit(training, config, known_at=known_at)
    target = _match(3, score=None)
    upstream = _upstream(training, target)
    prediction = first.predict(target, upstream, upstream_prediction_id="elo-prediction-1")

    assert first.parameters == second.parameters
    assert first.parameters.fifth_set_matches == 2
    assert first.parameters.fifth_set_home_wins == 1
    assert first.parameters.training_as_of == known_at
    assert len(first.parameters.training_result_revision_ids) == 3
    assert len(first.parameters.training_result_manifest_sha256) == 64
    assert first.parameters.training_cohort.stage == "regular"
    assert prediction.home_win_probability == pytest.approx(
        upstream.home_win_probability,
        abs=1e-12,
    )
    assert prediction.source_prediction_id == "elo-prediction-1"
    assert prediction.source_model_version == upstream.model_version
    assert prediction.source_config_id == upstream.config_id
    assert prediction.fitted_parameter_artifact_id == (
        first.parameters.fitted_parameter_artifact_id
    )
    assert prediction.model_version == "independent-sets-v2"
    assert prediction.config_id != first.parameters.config_id
    assert prediction.capabilities == (
        PredictionCapability.WINNER,
        PredictionCapability.SET_SCORE,
    )


def test_fit_rejects_any_mixed_cohort_dimension_and_future_training_row() -> None:
    training = [_match(0), _match(1)]
    with pytest.raises(ValueError, match="complete CohortKey"):
        SetOutcomePredictor.fit(
            training + [_match(2, stage="playoff")],
            known_at=START + timedelta(days=3),
        )
    with pytest.raises(ValueError, match="precede"):
        SetOutcomePredictor.fit(
            training + [_match(3)],
            known_at=START + timedelta(days=3, hours=-1),
        )


def test_predict_rejects_mismatched_or_post_cutoff_elo_artifacts() -> None:
    training = [_match(0), _match(1)]
    target = _match(2, score=None)
    predictor = SetOutcomePredictor.fit(
        training,
        known_at=target.prediction_cutoff_at,
    )
    upstream = _upstream(training, target)
    with pytest.raises(ValueError, match="schedule_revision_id"):
        predictor.predict(target, replace(upstream, schedule_revision_id="wrong"))

    later_predictor = SetOutcomePredictor.fit(
        training + [_match(2)],
        known_at=START + timedelta(days=3, hours=-1),
    )
    with pytest.raises(ValueError, match="after the prediction cutoff"):
        later_predictor.predict(target, upstream)


def test_prediction_contract_runs_schema_then_semantic_validation() -> None:
    training = [_match(0, score=(3, 2))]
    target = _match(1, score=None)
    predictor = SetOutcomePredictor.fit(
        training,
        known_at=target.prediction_cutoff_at,
    )
    prediction = predictor.predict(target, _upstream(training, target))
    output = prediction.to_contract(
        producer_variant_id="set-baseline-men-v2",
        input_snapshot_id="snapshot-1",
    )
    context = PredictionValidationContext(
        producer_variant_id="set-baseline-men-v2",
        input_snapshot_id="snapshot-1",
    )
    validate_prediction_output_v1(output, _schema(), context=context)

    invalid_documents = []
    wrong_mass = deepcopy(output)
    wrong_mass["set_score_probabilities"][0]["probability"] += 0.01
    invalid_documents.append(wrong_mass)
    wrong_marginal = deepcopy(output)
    wrong_marginal["home_win_probability"] = 0.1
    invalid_documents.append(wrong_marginal)
    wrong_capability = deepcopy(output)
    wrong_capability["capabilities"] = ["winner"]
    invalid_documents.append(wrong_capability)
    wrong_provenance = deepcopy(output)
    wrong_provenance["target_provenance"]["winner"]["upstream_config_id"] = "f" * 64
    invalid_documents.append(wrong_provenance)
    for invalid in invalid_documents:
        with pytest.raises(PredictionContractError):
            validate_prediction_output_v1(invalid, _schema(), context=context)

    reversed_output = deepcopy(output)
    reversed_output["set_score_probabilities"] = list(
        reversed(reversed_output["set_score_probabilities"])
    )
    with pytest.raises(PredictionContractError, match="schema violation"):
        validate_prediction_output_v1(reversed_output, _schema(), context=context)


def test_metrics_report_rps_wilson_calibration_coverage_and_assumption_error() -> None:
    training = [_match(0, score=(3, 2)), _match(1, score=(2, 3))]
    predictor = SetOutcomePredictor.fit(
        training,
        known_at=START + timedelta(days=2, hours=12),
    )
    matches = [
        _match(3, score=(3, 0)),
        _match(4, score=(2, 3)),
        _match(5, score=None),
        _match(6, score=(3, 1)),
    ]
    timeline = training + matches
    upstream = walk_forward_predictions(timeline, ELO_CONFIG)
    predictions = [
        predictor.predict(matches[0], upstream[matches[0].match_id]),
        predictor.predict(matches[1], upstream[matches[1].match_id]),
        predictor.predict(matches[2], upstream[matches[2].match_id]),
        None,
    ]
    report = evaluate_set_predictions(matches, predictions)

    assert report.metric_version == SET_METRIC_VERSION
    assert report.exact_score_tie_policy_version == EXACT_SCORE_TIE_POLICY_VERSION
    assert report.calibration_interval_version == CALIBRATION_INTERVAL_VERSION
    assert report.scheduled == 4
    assert report.predicted == 3
    assert report.result_available == 3
    assert report.evaluated == 2
    assert report.coverage == 0.5
    assert report.missing_reasons == {
        "prediction_unavailable": 1,
        "result_unavailable": 1,
    }
    assert report.ranked_probability_score is not None
    assert 0 <= report.ranked_probability_score <= 1
    assert sum(item.sample_size for item in report.winner_calibration) == 2
    assert all(
        0 <= item.uncertainty_lower <= item.observed_home_win_rate <= item.uncertainty_upper <= 1
        for item in report.winner_calibration
    )
    assert all(
        item.interval_version == CALIBRATION_INTERVAL_VERSION for item in report.winner_calibration
    )
    assert len(report.independence_assumption_error.outcomes) == 6


def test_exact_score_ties_are_scored_fractionally_and_symmetrically() -> None:
    training = [_match(0, score=(3, 2)), _match(1, score=(2, 3))]
    predictor = SetOutcomePredictor.fit(
        training,
        known_at=START + timedelta(days=2, hours=12),
    )
    home = _match(3, match_id="home-tie", score=(3, 1))
    away = _match(4, match_id="away-tie", score=(1, 3))
    upstream = walk_forward_predictions(training + [home, away], ELO_CONFIG)
    home_prediction = predictor.predict(
        home,
        replace(upstream[home.match_id], home_win_probability=0.5),
    )
    away_prediction = predictor.predict(
        away,
        replace(upstream[away.match_id], home_win_probability=0.5),
    )
    report = evaluate_set_predictions([home, away], [home_prediction, away_prediction])
    assert report.set_score_accuracy == pytest.approx(0.25)


def test_sequential_holdout_generates_elo_artifacts_and_keeps_cohorts_separate() -> None:
    scores = [(3, 2), (3, 1), (2, 3), (0, 3), (3, 2), (2, 3)]
    men = [_match(day, score=score) for day, score in enumerate(scores)]
    women = [
        _match(day, match_id=f"women-{day}", division=Division.WOMEN, score=score)
        for day, score in enumerate(reversed(scores))
    ]
    split = TemporalSplit(
        train_start=START - timedelta(hours=1),
        train_end=START + timedelta(days=2, hours=-1),
        validation_start=START + timedelta(days=2, hours=-1),
        validation_end=START + timedelta(days=4, hours=-1),
        test_start=START + timedelta(days=4, hours=-1),
        test_end=START + timedelta(days=6, hours=-1),
    )

    report = run_set_holdout(men, split, ELO_CONFIG)
    reversed_test = [
        _match(day, match_id=match.match_id, score=(3, 0))
        if match.prediction_cutoff_at >= split.validation_end
        else match
        for day, match in enumerate(men)
    ]
    changed = run_set_holdout(reversed_test, split, ELO_CONFIG)
    assert report.validation.scheduled == 2
    assert report.test.scheduled == 2
    assert report.test.evaluated == 2
    assert report.test_parameters_frozen is True
    assert report.test_parameters == changed.test_parameters
    assert report.test.ranked_probability_score != changed.test.ranked_probability_score
    assert report.elo_config.config_id == ELO_CONFIG.config_id

    with pytest.raises(ValueError, match="cohort dimensions"):
        run_set_holdout(men + women, split, ELO_CONFIG)
    suite = run_set_holdout_suite(men + women, split, ELO_CONFIG)
    assert {item.cohort.division for item in suite.reports} == {
        Division.MEN,
        Division.WOMEN,
    }
    assert all(item.test.evaluated == 2 for item in suite.reports)
