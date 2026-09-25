"""Joint score mathematics, contracts, market lines, and holdout tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import sqrt
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from vlytics.engine.experiments.baselines import TemporalSplit
from vlytics.engine.experiments.points import (
    ObservedMatchPoints,
    evaluate_joint_score_predictions,
    run_joint_score_holdout,
    run_joint_score_holdout_suite,
)
from vlytics.engine.features import AvailabilityPolicy
from vlytics.engine.prediction_validation import (
    JointScoreReferenceMetadata,
    PredictionContractError,
    PredictionValidationContext,
    validate_prediction_output_v1,
)
from vlytics.engine.predictors.elo import (
    Division,
    EloMatch,
    ResultFinalityPolicy,
    TimingEligibility,
)
from vlytics.engine.predictors.points import (
    DEFAULT_SCORE_RULE_VERSION,
    DistributionDiagnostics,
    DistributionMethod,
    JointScoreAtom,
    JointScoreDistribution,
    JointScorePredictor,
    PointModelConfig,
    SetPointScore,
    VerifiedSeasonScoreRule,
    VerifiedSetPredictionArtifact,
    build_joint_score_distribution,
    conditional_set_score_distribution,
    is_feasible_match_total,
    is_valid_set_score,
    set_win_probability_from_rally,
    solve_rally_probability,
)
from vlytics.engine.predictors.sets import (
    SET_OUTCOME_ORDER,
    PredictionCapability,
    SetOutcome,
    SetOutcomeDistribution,
    SetOutcomePrediction,
    SetOutcomeProbability,
    SetTrainingCohort,
    independent_set_distribution,
)

START = datetime(2030, 1, 1, tzinfo=UTC)


def _set_prediction(
    match: EloMatch | None = None,
    *,
    division: Division = Division.MEN,
    regular_probability: float = 0.57,
    deciding_probability: float = 0.53,
    training_as_of: datetime | None = None,
    training_manifest_sha256: str | None = None,
) -> SetOutcomePrediction:
    selected_match = match or _match(0, division=division, match_id="match")
    distribution = independent_set_distribution(regular_probability, deciding_probability)
    return SetOutcomePrediction(
        match_id=selected_match.match_id,
        schedule_revision_id=selected_match.schedule_revision_id,
        prediction_cutoff_at=selected_match.prediction_cutoff_at,
        division=selected_match.division,
        source_home_win_probability=distribution.home_win_probability,
        source_prediction_id=f"elo-{selected_match.match_id}",
        source_model_version="elo-test-v1",
        source_config_id="b" * 64,
        regular_set_home_win_probability=regular_probability,
        fifth_set_home_win_probability=deciding_probability,
        distribution=distribution,
        model_version="set-model-test-v1",
        distribution_version="set-score-six-v1",
        config_id="a" * 64,
        random_seed=0,
        capabilities=(PredictionCapability.WINNER, PredictionCapability.SET_SCORE),
        fitted_parameter_artifact_id="c" * 64,
        training_cohort=SetTrainingCohort(
            division=selected_match.division,
            availability_policy="historical_reconstruction",
            competition="regular",
            stage="regular",
            timing_eligibility="reconstructed",
            result_finality_policy="final_only",
            input_version="elo-match-v2",
            franchise_mapping_version="mapping-v1",
        ),
        training_as_of=training_as_of or START - timedelta(days=1),
        training_result_manifest_sha256=(
            training_manifest_sha256 or hashlib.sha256(b"[]").hexdigest()
        ),
    )


def _set_artifact(prediction: SetOutcomePrediction) -> VerifiedSetPredictionArtifact:
    return VerifiedSetPredictionArtifact.capture(
        prediction,
        producer_variant_id="set-baseline-v2",
        input_snapshot_id=f"snapshot-{prediction.match_id}",
    )


def _rule_identity(division: Division = Division.MEN) -> VerifiedSeasonScoreRule:
    return VerifiedSeasonScoreRule(
        season_id="s1",
        division=division,
        rule_version=DEFAULT_SCORE_RULE_VERSION,
        mirror_rule_artifact_id=("e" if division is Division.MEN else "f") * 64,
        verified_at=START - timedelta(days=2),
    )


def _point_config(**overrides: object) -> PointModelConfig:
    values: dict[str, object] = {
        "score_rule_version": DEFAULT_SCORE_RULE_VERSION,
    }
    values.update(overrides)
    return PointModelConfig(**values)  # type: ignore[arg-type]


def _match(day: int, *, division: Division, match_id: str) -> EloMatch:
    scheduled_start = START + timedelta(days=day)
    return EloMatch(
        match_id=match_id,
        schedule_revision_id=f"schedule-{match_id}",
        schedule_revision=1,
        schedule_observed_at=scheduled_start - timedelta(hours=2),
        schedule_raw_snapshot_sha256="a" * 64,
        prediction_cutoff_at=scheduled_start - timedelta(hours=1),
        scheduled_start_at=scheduled_start,
        season_id="s1",
        competition="regular",
        stage="regular",
        division=division,
        home_team_id="H",
        away_team_id="A",
        availability_policy=AvailabilityPolicy.HISTORICAL_RECONSTRUCTION,
        timing_eligibility=TimingEligibility.RECONSTRUCTED,
        result_finality_policy=ResultFinalityPolicy.FINAL_ONLY,
        franchise_mapping_version="mapping-v1",
    )


def _observed_sweep(match_id: str, division: Division, *, home_won: bool) -> ObservedMatchPoints:
    score = SetPointScore(25, 0) if home_won else SetPointScore(0, 25)
    return ObservedMatchPoints(
        match_id=match_id,
        division=division,
        set_scores=(score, score, score),
        score_rule_version=DEFAULT_SCORE_RULE_VERSION,
    )


def _manual_distribution() -> JointScoreDistribution:
    source = SetOutcomeDistribution(
        tuple(
            SetOutcomeProbability(
                outcome,
                0.4
                if outcome is SetOutcome.HOME_3_0
                else 0.6
                if outcome is SetOutcome.AWAY_3_0
                else 0.0,
            )
            for outcome in (
                SetOutcome.HOME_3_0,
                SetOutcome.HOME_3_1,
                SetOutcome.HOME_3_2,
                SetOutcome.AWAY_3_2,
                SetOutcome.AWAY_3_1,
                SetOutcome.AWAY_3_0,
            )
        )
    )
    source_prediction = replace(
        _set_prediction(),
        distribution=source,
        source_home_win_probability=source.home_win_probability,
    )
    source_artifact = _set_artifact(source_prediction)
    config = _point_config(model_version="manual-v1", random_seed=7, max_deuce_cycles=0)
    rule_identity = _rule_identity()
    home_tail = conditional_set_score_distribution(
        source_prediction.regular_set_home_win_probability,
        home_won=True,
        target=rule_identity.rules.normal_target,
        max_deuce_cycles=0,
    ).conditional_tail_probability
    away_tail = conditional_set_score_distribution(
        source_prediction.regular_set_home_win_probability,
        home_won=False,
        target=rule_identity.rules.normal_target,
        max_deuce_cycles=0,
    ).conditional_tail_probability
    tail_error = 0.4 * (1.0 - (1.0 - home_tail) ** 3) + 0.6 * (1.0 - (1.0 - away_tail) ** 3)
    return JointScoreDistribution(
        entries=(
            JointScoreAtom(SetOutcome.HOME_3_0, 75, 0, 0.4),
            JointScoreAtom(SetOutcome.AWAY_3_0, 0, 75, 0.6),
        ),
        source_set_distribution=source,
        division=Division.MEN,
        source_set_artifact=source_artifact,
        producer_variant_id="stat-joint-v1",
        input_snapshot_id=source_artifact.input_snapshot_id,
        model_version=config.model_version,
        distribution_version=config.distribution_version,
        config_id=config.config_id,
        score_rule_identity=rule_identity,
        score_rules=rule_identity.rules,
        method=config.method,
        random_seed=config.random_seed,
        sample_count=0,
        configured_sample_count=config.sample_count,
        max_deuce_cycles=config.max_deuce_cycles,
        max_monte_carlo_set_marginal_error=(config.max_monte_carlo_set_marginal_error),
        diagnostics=DistributionDiagnostics(
            0.0,
            0.0,
            tail_error,
            max(home_tail, away_tail),
            0.0,
        ),
    )


@pytest.mark.parametrize(
    ("score", "target", "valid"),
    [
        (SetPointScore(25, 0), 25, True),
        (SetPointScore(25, 23), 25, True),
        (SetPointScore(26, 24), 25, True),
        (SetPointScore(38, 36), 25, True),
        (SetPointScore(15, 13), 15, True),
        (SetPointScore(25, 24), 25, False),
        (SetPointScore(26, 23), 25, False),
        (SetPointScore(24, 22), 25, False),
    ],
)
def test_set_score_rules_reject_impossible_scores(
    score: SetPointScore,
    target: int,
    valid: bool,
) -> None:
    assert is_valid_set_score(score, target) is valid


def test_rally_probability_inversion_is_analytic_for_normal_and_deciding_sets() -> None:
    for target in (25, 15):
        for expected in (0.01, 0.25, 0.5, 0.72, 0.99):
            rally = solve_rally_probability(expected, target)
            assert set_win_probability_from_rally(rally, target) == pytest.approx(
                expected,
                abs=1e-12,
            )


def test_conditional_set_distribution_has_mass_one_and_records_deuce_tail() -> None:
    short = conditional_set_score_distribution(
        0.5,
        home_won=True,
        target=25,
        max_deuce_cycles=0,
    )
    long = conditional_set_score_distribution(
        0.5,
        home_won=True,
        target=25,
        max_deuce_cycles=8,
    )
    away = conditional_set_score_distribution(
        0.5,
        home_won=False,
        target=25,
        max_deuce_cycles=8,
    )

    assert sum(item.probability for item in long.outcomes) == pytest.approx(1.0)
    assert SetPointScore(26, 24) in short.as_mapping()
    assert SetPointScore(27, 25) not in short.as_mapping()
    assert long.conditional_tail_probability < short.conditional_tail_probability
    assert long.conditional_tail_probability > 0
    assert {
        (item.score.away_points, item.score.home_points): item.probability for item in away.outcomes
    } == pytest.approx(
        {
            (item.score.home_points, item.score.away_points): item.probability
            for item in long.outcomes
        }
    )


def test_exact_joint_distribution_has_total_mass_and_matching_set_marginal() -> None:
    set_prediction = _set_prediction()
    config = _point_config(max_deuce_cycles=2)
    prediction = JointScorePredictor(
        config,
        producer_variant_id="stat-joint-v1",
    ).predict(_set_artifact(set_prediction), _rule_identity())
    distribution = prediction.distribution

    assert sum(item.probability for item in distribution.entries) == pytest.approx(
        1.0,
        abs=1e-12,
    )
    assert distribution.set_score_distribution.probabilities == pytest.approx(
        set_prediction.distribution.probabilities,
        abs=1e-12,
    )
    assert distribution.home_win_probability == pytest.approx(
        set_prediction.home_win_probability,
        abs=1e-12,
    )
    assert distribution.diagnostics.source_set_marginal_max_abs_error < 1e-12
    assert distribution.diagnostics.deuce_truncation_error_upper_bound > 0
    assert all(
        is_feasible_match_total(
            atom.set_score,
            atom.home_points,
            atom.away_points,
            distribution.score_rules,
            distribution.max_deuce_cycles,
        )
        for atom in distribution.entries
    )


def test_joint_distribution_rejects_an_impossible_aggregate_score() -> None:
    valid = _manual_distribution()
    with pytest.raises(ValueError, match="impossible match score"):
        replace(
            valid,
            entries=(JointScoreAtom(SetOutcome.HOME_3_0, 10, 20, 1.0),),
        )


def test_joint_distribution_recomputes_diagnostics_and_config_identity() -> None:
    valid = _manual_distribution()
    with pytest.raises(ValueError, match="recorded source set marginal error"):
        replace(
            valid,
            diagnostics=replace(
                valid.diagnostics,
                source_set_marginal_max_abs_error=0.1,
            ),
        )
    with pytest.raises(ValueError, match="config_id is inconsistent"):
        replace(valid, config_id="0" * 64)
    with pytest.raises(ValueError, match="deuce truncation error"):
        replace(
            valid,
            diagnostics=replace(
                valid.diagnostics,
                deuce_truncation_error_upper_bound=0.0,
            ),
        )


def test_total_handicap_and_push_are_hand_calculated_from_the_same_joint_pmf() -> None:
    distribution = _manual_distribution()

    assert distribution.point_total(75).to_dict() == {
        "above": 0.0,
        "below": 0.0,
        "push": 1.0,
    }
    assert distribution.point_total(74.5).to_dict() == {
        "above": 1.0,
        "below": 0.0,
        "push": 0.0,
    }
    assert distribution.point_handicap(75).to_dict() == {
        "above": 0.4,
        "below": 0.0,
        "push": 0.6,
    }
    assert distribution.point_handicap(0).to_dict() == {
        "above": 0.4,
        "below": 0.6,
        "push": 0.0,
    }
    assert distribution.point_total_distribution == {75: 1.0}
    assert distribution.point_differential_distribution == {-75: 0.6, 75: 0.4}


def test_home_away_symmetry_preserves_joint_points_and_reverses_set_scores() -> None:
    config = _point_config(max_deuce_cycles=1)
    home = build_joint_score_distribution(
        _set_artifact(
            _set_prediction(
                regular_probability=0.63,
                deciding_probability=0.57,
            )
        ),
        _rule_identity(),
        producer_variant_id="stat-joint-v1",
        config=config,
    )
    away = build_joint_score_distribution(
        _set_artifact(
            _set_prediction(
                regular_probability=0.37,
                deciding_probability=0.43,
            )
        ),
        _rule_identity(),
        producer_variant_id="stat-joint-v1",
        config=config,
    )
    away_mapping = {
        (atom.set_score, atom.home_points, atom.away_points): atom.probability
        for atom in away.entries
    }
    reverse_outcome = dict(zip(SET_OUTCOME_ORDER, reversed(SET_OUTCOME_ORDER), strict=True))
    for atom in home.entries:
        assert away_mapping[
            (reverse_outcome[atom.set_score], atom.away_points, atom.home_points)
        ] == pytest.approx(atom.probability, abs=1e-14)


def test_monte_carlo_output_is_seeded_versioned_and_records_sampling_error() -> None:
    source_artifact = _set_artifact(
        _set_prediction(
            regular_probability=0.56,
            deciding_probability=0.51,
        )
    )
    config = _point_config(
        method=DistributionMethod.MONTE_CARLO,
        random_seed=91,
        sample_count=2_000,
        max_deuce_cycles=1,
    )
    first = build_joint_score_distribution(
        source_artifact,
        _rule_identity(),
        producer_variant_id="stat-joint-v1",
        config=config,
    )
    second = build_joint_score_distribution(
        source_artifact,
        _rule_identity(),
        producer_variant_id="stat-joint-v1",
        config=config,
    )
    changed = build_joint_score_distribution(
        source_artifact,
        _rule_identity(),
        producer_variant_id="stat-joint-v1",
        config=replace(config, random_seed=92),
    )

    assert first.to_artifact() == second.to_artifact()
    assert first.distribution_id != changed.distribution_id
    assert first.sample_count == 2_000
    assert first.diagnostics.total_mass_error < 1e-12
    assert first.diagnostics.monte_carlo_max_standard_error_95 == pytest.approx(0.98 / sqrt(2_000))
    assert (
        first.diagnostics.source_set_marginal_max_abs_error
        <= config.max_monte_carlo_set_marginal_error
    )

    with pytest.raises(ValueError, match="quality threshold"):
        build_joint_score_distribution(
            source_artifact,
            _rule_identity(),
            producer_variant_id="stat-joint-v1",
            config=replace(config, max_monte_carlo_set_marginal_error=1e-9),
        )


def test_joint_artifact_and_four_capability_prediction_validate_against_schemas() -> None:
    source_artifact = _set_artifact(_set_prediction())
    prediction = JointScorePredictor(
        _point_config(max_deuce_cycles=0),
        producer_variant_id="stat-joint-v1",
    ).predict(source_artifact, _rule_identity())
    repository_root = Path(__file__).resolve().parents[3]
    joint_schema = json.loads(
        (repository_root / "contracts" / "joint-score-v1.schema.json").read_text(encoding="utf-8")
    )
    prediction_schema = json.loads(
        (repository_root / "contracts" / "prediction-v1.schema.json").read_text(encoding="utf-8")
    )
    artifact = prediction.distribution.to_artifact()
    contract = prediction.to_contract()

    Draft202012Validator(joint_schema).validate(artifact)
    Draft202012Validator(prediction_schema).validate(contract)
    assert artifact["source_set_prediction"] == source_artifact.to_lineage_dict()
    assert artifact["verified_score_rule"] == _rule_identity().to_dict()
    assert contract["capabilities"] == [
        "winner",
        "set_score",
        "point_handicap",
        "point_total",
    ]
    assert contract["joint_score_distribution_ref"] == artifact["distribution_id"]
    assert set(contract["target_provenance"]) == set(contract["capabilities"])
    metadata = JointScoreReferenceMetadata(
        distribution_id=prediction.distribution.distribution_id,
        producer_variant_id="stat-joint-v1",
        input_snapshot_id=source_artifact.input_snapshot_id,
        home_win_probability=prediction.distribution.home_win_probability,
        set_score_probabilities=(prediction.distribution.set_score_distribution.probabilities),
    )
    validate_prediction_output_v1(
        contract,
        prediction_schema,
        context=PredictionValidationContext(
            producer_variant_id="stat-joint-v1",
            input_snapshot_id=source_artifact.input_snapshot_id,
            joint_score_resolver=lambda reference: (
                metadata if reference == metadata.distribution_id else None
            ),
        ),
    )
    with pytest.raises(PredictionContractError, match="another variant"):
        validate_prediction_output_v1(
            contract,
            prediction_schema,
            context=PredictionValidationContext(
                producer_variant_id="stat-joint-v1",
                input_snapshot_id=source_artifact.input_snapshot_id,
                joint_score_resolver=lambda _: replace(
                    metadata,
                    producer_variant_id="other-variant",
                ),
            ),
        )

    invalid = dict(contract)
    invalid["target_provenance"] = {
        key: value
        for key, value in contract["target_provenance"].items()
        if key != "point_handicap"
    }
    assert list(Draft202012Validator(prediction_schema).iter_errors(invalid))

    with pytest.raises(ValueError, match="input_snapshot_id"):
        prediction.to_contract(input_snapshot_id="snapshot-other")
    with pytest.raises(ValueError, match="producer_variant_id"):
        prediction.to_contract(producer_variant_id="other-variant")

    changed_source = _set_artifact(replace(source_artifact.prediction, source_config_id="d" * 64))
    changed_prediction = JointScorePredictor(
        _point_config(max_deuce_cycles=0),
        producer_variant_id="stat-joint-v1",
    ).predict(changed_source, _rule_identity())
    assert changed_prediction.distribution.distribution_id != artifact["distribution_id"]


def test_observed_match_points_rejects_invalid_or_post_completion_sets() -> None:
    with pytest.raises(ValueError, match="invalid set score"):
        ObservedMatchPoints(
            match_id="invalid",
            division=Division.MEN,
            set_scores=(
                SetPointScore(25, 20),
                SetPointScore(25, 24),
                SetPointScore(25, 18),
            ),
            score_rule_version=DEFAULT_SCORE_RULE_VERSION,
        )
    with pytest.raises(ValueError, match="cannot continue"):
        ObservedMatchPoints(
            match_id="too-long",
            division=Division.WOMEN,
            set_scores=(
                SetPointScore(25, 0),
                SetPointScore(25, 0),
                SetPointScore(25, 0),
                SetPointScore(0, 25),
            ),
            score_rule_version=DEFAULT_SCORE_RULE_VERSION,
        )


def test_men_and_women_holdout_reports_distribution_metrics_constraints_and_n() -> None:
    matches: list[EloMatch] = []
    split = TemporalSplit(
        train_start=START - timedelta(hours=1),
        train_end=START + timedelta(days=2, hours=-1),
        validation_start=START + timedelta(days=2, hours=-1),
        validation_end=START + timedelta(days=4, hours=-1),
        test_start=START + timedelta(days=4, hours=-1),
        test_end=START + timedelta(days=6, hours=-1),
    )
    set_predictions: list[VerifiedSetPredictionArtifact] = []
    results: list[ObservedMatchPoints] = []
    for division, probability in ((Division.MEN, 1.0), (Division.WOMEN, 0.0)):
        for day in range(6):
            match_id = f"{division.value}-{day}"
            match = _match(day, division=division, match_id=match_id)
            matches.append(match)
            training_as_of = (
                START - timedelta(days=1)
                if day < 2
                else split.train_end
                if day < 4
                else split.validation_end
            )
            set_predictions.append(
                _set_artifact(
                    _set_prediction(
                        match,
                        regular_probability=probability,
                        deciding_probability=probability,
                        training_as_of=training_as_of,
                    )
                )
            )
            results.append(
                _observed_sweep(
                    match_id,
                    division,
                    home_won=division is Division.MEN,
                )
            )
    suite = run_joint_score_holdout_suite(
        matches,
        set_predictions,
        results,
        split,
        _point_config(max_deuce_cycles=0),
        producer_variant_id="stat-joint-v1",
        score_rule_identities={
            ("s1", Division.MEN): _rule_identity(Division.MEN),
            ("s1", Division.WOMEN): _rule_identity(Division.WOMEN),
        },
    )

    assert {report.cohort.division for report in suite.reports} == {
        Division.MEN,
        Division.WOMEN,
    }
    for report in suite.reports:
        assert report.validation.evaluated == 2
        assert report.test.evaluated == 2
        assert report.test.coverage == 1.0
        assert report.test.predicted_constraint_violations == 0
        assert report.test.out_of_support_results == 0
        assert report.test.joint_log_loss == pytest.approx(0.0)
        assert report.test.point_total_crps == pytest.approx(0.0)
        assert report.test.point_differential_crps == pytest.approx(0.0)
        assert len(report.validation_configuration_evidence.development_match_ids) == 2
        assert len(report.test_configuration_evidence.development_match_ids) == 4


def test_holdout_rejects_identity_leakage_unfrozen_models_and_outside_rows() -> None:
    split = TemporalSplit(
        train_start=START - timedelta(hours=1),
        train_end=START + timedelta(days=2, hours=-1),
        validation_start=START + timedelta(days=2, hours=-1),
        validation_end=START + timedelta(days=4, hours=-1),
        test_start=START + timedelta(days=4, hours=-1),
        test_end=START + timedelta(days=6, hours=-1),
    )
    matches = [
        _match(day, division=Division.MEN, match_id=f"holdout-{day}") for day in (0, 2, 3, 4)
    ]
    artifacts: list[VerifiedSetPredictionArtifact | None] = [
        None,
        _set_artifact(_set_prediction(matches[1], training_as_of=split.train_end)),
        _set_artifact(_set_prediction(matches[2], training_as_of=split.train_end)),
        _set_artifact(_set_prediction(matches[3], training_as_of=split.validation_end)),
    ]
    results: list[ObservedMatchPoints | None] = [None] * len(matches)
    rules = {("s1", Division.MEN): _rule_identity()}
    config = _point_config(max_deuce_cycles=0)

    schedule_mismatch = list(artifacts)
    assert schedule_mismatch[1] is not None
    schedule_mismatch[1] = _set_artifact(
        replace(
            schedule_mismatch[1].prediction,
            schedule_revision_id="schedule-other",
        )
    )
    with pytest.raises(ValueError, match="schedule_revision_id"):
        run_joint_score_holdout(
            matches,
            schedule_mismatch,
            results,
            split,
            config,
            producer_variant_id="stat-joint-v1",
            score_rule_identities=rules,
        )

    leaky = list(artifacts)
    assert leaky[1] is not None
    leaky[1] = _set_artifact(
        replace(
            leaky[1].prediction,
            training_as_of=matches[1].prediction_cutoff_at + timedelta(minutes=1),
        )
    )
    with pytest.raises(ValueError, match="training_as_of"):
        run_joint_score_holdout(
            matches,
            leaky,
            results,
            split,
            config,
            producer_variant_id="stat-joint-v1",
            score_rule_identities=rules,
        )

    unfrozen = list(artifacts)
    assert unfrozen[2] is not None
    unfrozen[2] = _set_artifact(replace(unfrozen[2].prediction, config_id="d" * 64))
    with pytest.raises(ValueError, match="frozen lineage"):
        run_joint_score_holdout(
            matches,
            unfrozen,
            results,
            split,
            config,
            producer_variant_id="stat-joint-v1",
            score_rule_identities=rules,
        )

    outside = _match(6, division=Division.MEN, match_id="outside")
    with pytest.raises(ValueError, match="outside the declared split"):
        run_joint_score_holdout(
            [*matches, outside],
            [*artifacts, None],
            [*results, None],
            split,
            config,
            producer_variant_id="stat-joint-v1",
            score_rule_identities=rules,
        )

    with pytest.raises(ValueError, match="verified season score rule identity is required"):
        run_joint_score_holdout(
            matches,
            artifacts,
            results,
            split,
            config,
            producer_variant_id="stat-joint-v1",
            score_rule_identities={},
        )


def test_metric_report_tracks_missing_rows_and_tail_constraints() -> None:
    match = _match(0, division=Division.MEN, match_id="missing")
    prediction = JointScorePredictor(
        _point_config(max_deuce_cycles=0),
        producer_variant_id="stat-joint-v1",
    ).predict(_set_artifact(_set_prediction(match)), _rule_identity())
    report = evaluate_joint_score_predictions(
        [match, replace(match, match_id="missing-2", schedule_revision_id="schedule-2")],
        [prediction, None],
        [None, None],
    )

    assert report.scheduled == 2
    assert report.predicted == 1
    assert report.evaluated == 0
    assert report.coverage == 0.0
    assert report.missing_predictions == 1
    assert report.missing_results == 2
    assert report.predicted_constraint_violations == 0
    assert report.mean_deuce_truncation_error_upper_bound is not None
