"""Market contract, as-of selection, probability, and settlement tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pytest

from vlytics.engine.features import AvailabilityPolicy
from vlytics.engine.market import (
    AvailabilityStatus,
    EvaluationEligibility,
    MarketAvailability,
    MarketContractError,
    MarketEvaluator,
    MarketValidationContext,
    MissingMarketAdapter,
    PredictionMarketInput,
    ResultFinality,
    ResultRevision,
    SettlementOutcome,
    SyntheticMarketAdapter,
    default_market_adapter,
    validate_market_snapshot_v1,
)
from vlytics.engine.predictors.elo import (
    Division,
    EloMatch,
    ResultFinalityPolicy,
    TimingEligibility,
)
from vlytics.engine.predictors.points import (
    DEFAULT_SCORE_RULE_VERSION,
    JointScoreDistribution,
    PointModelConfig,
    VerifiedSeasonScoreRule,
    VerifiedSetPredictionArtifact,
    build_joint_score_distribution,
)
from vlytics.engine.predictors.sets import (
    PredictionCapability,
    SetOutcomePrediction,
    SetTrainingCohort,
    independent_set_distribution,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPOSITORY_ROOT / "contracts" / "market-v1.schema.json"
FIXTURE_DIRECTORY = REPOSITORY_ROOT / "fixtures" / "synthetic" / "market"
MATCH_ID = "20000000-0000-4000-8000-000000000001"


def _document(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIRECTORY / name).read_text(encoding="utf-8"))


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _snapshot(name: str = "full-match-v1.json"):
    return validate_market_snapshot_v1(
        _document(name),
        _schema(),
        context=MarketValidationContext(
            match_id=MATCH_ID,
            source_event_resolver=lambda source, source_event_id: (
                MATCH_ID
                if source == "synthetic-market"
                and source_event_id in {"synthetic-event-001", "synthetic-event-002"}
                else None
            ),
        ),
    )


@lru_cache(maxsize=1)
def _joint_distribution() -> JointScoreDistribution:
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    match = EloMatch(
        match_id=MATCH_ID,
        schedule_revision_id="schedule-market-1",
        schedule_revision=1,
        schedule_observed_at=cutoff - timedelta(hours=1),
        schedule_raw_snapshot_sha256="a" * 64,
        prediction_cutoff_at=cutoff,
        scheduled_start_at=cutoff + timedelta(hours=1),
        season_id="season-1",
        competition="regular",
        stage="regular",
        division=Division.MEN,
        home_team_id="home",
        away_team_id="away",
        availability_policy=AvailabilityPolicy.HISTORICAL_RECONSTRUCTION,
        timing_eligibility=TimingEligibility.RECONSTRUCTED,
        result_finality_policy=ResultFinalityPolicy.FINAL_ONLY,
        franchise_mapping_version="mapping-v1",
    )
    set_distribution = independent_set_distribution(0.57, 0.53)
    set_prediction = SetOutcomePrediction(
        match_id=match.match_id,
        schedule_revision_id=match.schedule_revision_id,
        prediction_cutoff_at=match.prediction_cutoff_at,
        division=match.division,
        source_home_win_probability=set_distribution.home_win_probability,
        source_prediction_id="elo-market-1",
        source_model_version="elo-test-v1",
        source_config_id="b" * 64,
        regular_set_home_win_probability=0.57,
        fifth_set_home_win_probability=0.53,
        distribution=set_distribution,
        model_version="set-test-v1",
        distribution_version="set-score-six-v1",
        config_id="c" * 64,
        random_seed=0,
        capabilities=(PredictionCapability.WINNER, PredictionCapability.SET_SCORE),
        fitted_parameter_artifact_id="d" * 64,
        training_cohort=SetTrainingCohort(
            division=Division.MEN,
            availability_policy="historical_reconstruction",
            competition="regular",
            stage="regular",
            timing_eligibility="reconstructed",
            result_finality_policy="final_only",
            input_version="elo-match-v2",
            franchise_mapping_version="mapping-v1",
        ),
        training_as_of=cutoff - timedelta(days=1),
        training_result_manifest_sha256=hashlib.sha256(b"[]").hexdigest(),
    )
    artifact = VerifiedSetPredictionArtifact.capture(
        set_prediction,
        producer_variant_id="set-baseline-v2",
        input_snapshot_id="snapshot-market-1",
    )
    rule = VerifiedSeasonScoreRule(
        season_id="season-1",
        division=Division.MEN,
        rule_version=DEFAULT_SCORE_RULE_VERSION,
        mirror_rule_artifact_id="e" * 64,
        verified_at=cutoff - timedelta(days=2),
    )
    return build_joint_score_distribution(
        artifact,
        rule,
        producer_variant_id="stat-joint-v1",
        config=PointModelConfig(
            score_rule_version=DEFAULT_SCORE_RULE_VERSION,
            model_version="market-joint-v1",
            max_deuce_cycles=0,
        ),
    )


def _prediction() -> PredictionMarketInput:
    distribution = _joint_distribution()
    return PredictionMarketInput.from_joint_score_distribution(
        prediction_id="prediction-1",
        match_id=MATCH_ID,
        input_cutoff_at=distribution.source_set_artifact.prediction.prediction_cutoff_at,
        distribution=distribution,
        producer_variant_id=distribution.producer_variant_id,
        input_snapshot_id=distribution.input_snapshot_id,
    )


def _line_map(values):
    return {line.line_id: line for line in values}


def test_prediction_market_input_is_factory_only_and_ownership_checked() -> None:
    distribution = _joint_distribution()
    with pytest.raises(TypeError):
        PredictionMarketInput()  # type: ignore[call-arg]
    with pytest.raises(MarketContractError, match="another variant"):
        PredictionMarketInput.from_joint_score_distribution(
            prediction_id="prediction-1",
            match_id=MATCH_ID,
            input_cutoff_at=distribution.source_set_artifact.prediction.prediction_cutoff_at,
            distribution=distribution,
            producer_variant_id="wrong-variant",
            input_snapshot_id=distribution.input_snapshot_id,
        )
    with pytest.raises(TypeError):
        PredictionMarketInput.from_joint_score_distribution(
            prediction_id="prediction-1",
            match_id=MATCH_ID,
            input_cutoff_at=distribution.source_set_artifact.prediction.prediction_cutoff_at,
            distribution=distribution,
            producer_variant_id=distribution.producer_variant_id,
            input_snapshot_id=distribution.input_snapshot_id,
            set_score_probabilities={"3:0": 1.0},  # type: ignore[call-arg]
        )
    resolved = PredictionMarketInput.from_resolver(
        prediction_id="prediction-1",
        match_id=MATCH_ID,
        input_cutoff_at=distribution.source_set_artifact.prediction.prediction_cutoff_at,
        joint_score_distribution_id=distribution.distribution_id,
        producer_variant_id=distribution.producer_variant_id,
        input_snapshot_id=distribution.input_snapshot_id,
        resolver=lambda distribution_id: (
            distribution if distribution_id == distribution.distribution_id else None
        ),
    )
    assert resolved.joint_score_distribution_id == distribution.distribution_id
    assert resolved.home_win_probability == distribution.home_win_probability
    assert dict(resolved.point_totals) == dict(distribution.point_total_distribution)


def test_market_schema_rejects_non_rfc3339_datetime() -> None:
    document = _document("full-match-v1.json")
    document["quoted_at"] = "2026-09-20 08:00:00"
    with pytest.raises(MarketContractError, match="date-time"):
        validate_market_snapshot_v1(document, _schema())


def test_market_fixture_validates_source_mapping_and_canonical_hash() -> None:
    snapshot = _snapshot()
    assert snapshot.match_id == MATCH_ID
    assert snapshot.canonical_sha256 == snapshot.sha256
    with pytest.raises(MarketContractError, match="another match"):
        validate_market_snapshot_v1(
            _document("full-match-v1.json"),
            _schema(),
            context=MarketValidationContext(
                source_event_resolver=lambda source, source_event_id: "different-match"
            ),
        )


def test_missing_adapter_is_default_and_prediction_input_has_no_market_fields() -> None:
    adapter = default_market_adapter()
    assert isinstance(adapter, MissingMarketAdapter)
    availability = adapter.snapshot_as_of(
        match_id=MATCH_ID,
        cutoff_at=datetime(2026, 9, 20, 8, 30, tzinfo=UTC),
        max_age=timedelta(hours=1),
    )
    assert availability.status is AvailabilityStatus.MISSING
    assert not any("market" in field.name for field in fields(PredictionMarketInput))


def test_as_of_selection_rejects_late_receipt_and_enforces_max_age() -> None:
    snapshot = _snapshot()
    adapter = SyntheticMarketAdapter(
        [snapshot],
        source_event_mappings={(snapshot.source, snapshot.source_event_id): MATCH_ID},
    )
    before_receipt = adapter.snapshot_as_of(
        match_id=MATCH_ID,
        cutoff_at=datetime(2026, 9, 20, 8, 0, 7, tzinfo=UTC),
        max_age=timedelta(hours=1),
    )
    assert before_receipt.status is AvailabilityStatus.MISSING
    assert before_receipt.reason == "late_quote_received_after_cutoff"

    stale = adapter.snapshot_as_of(
        match_id=MATCH_ID,
        cutoff_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
        max_age=timedelta(minutes=30),
    )
    assert stale.status is AvailabilityStatus.STALE


def test_post_prediction_probabilities_cover_winner_handicap_and_totals() -> None:
    snapshot = _snapshot()
    prediction = _prediction()
    evaluation = MarketEvaluator().evaluate(
        prediction,
        MarketAvailability(
            status=AvailabilityStatus.AVAILABLE,
            reason="fixture",
            snapshot=snapshot,
        ),
    )
    assert evaluation.eligibility is EvaluationEligibility.ELIGIBLE
    lines = _line_map(evaluation.lines)
    assert lines["moneyline-home"].probabilities.win == pytest.approx(
        prediction.home_win_probability
    )
    assert lines["moneyline-away"].probabilities.win == pytest.approx(
        1.0 - prediction.home_win_probability
    )
    # Home -1.5 sets covers exactly 3:0 and 3:1 from the stored joint marginal.
    expected_set_cover = sum(
        prediction.set_score_probabilities[outcome] for outcome in ("3:0", "3:1")
    )
    assert lines["sets-home-minus-1.5"].probabilities.win == pytest.approx(expected_set_cover)
    assert lines["sets-home-minus-1.5"].probabilities.push == 0
    expected_point_cover = sum(
        probability
        for differential, probability in prediction.point_differentials.items()
        if differential - 5 > 0
    )
    expected_point_push = prediction.point_differentials.get(5, 0.0)
    assert lines["points-home-minus-5"].probabilities.win == pytest.approx(expected_point_cover)
    assert lines["points-home-minus-5"].probabilities.push == pytest.approx(expected_point_push)
    expected_over = sum(
        probability for total, probability in prediction.point_totals.items() if total > 180
    )
    expected_total_push = prediction.point_totals.get(180, 0.0)
    assert lines["points-over-180"].probabilities.win == pytest.approx(expected_over)
    assert lines["points-over-180"].probabilities.push == pytest.approx(expected_total_push)
    expected_set_over = sum(
        probability
        for outcome, probability in prediction.set_score_probabilities.items()
        if sum(int(value) for value in outcome.split(":")) > 4
    )
    expected_set_push = sum(
        probability
        for outcome, probability in prediction.set_score_probabilities.items()
        if sum(int(value) for value in outcome.split(":")) == 4
    )
    assert lines["sets-over-4"].probabilities.win == pytest.approx(expected_set_over)
    assert lines["sets-over-4"].probabilities.push == pytest.approx(expected_set_push)


def test_settlement_records_exact_result_revision_and_win_loss_push_void() -> None:
    snapshot = _snapshot()
    result = ResultRevision(
        match_id=MATCH_ID,
        result_revision_id="result-revision-7",
        revision=7,
        finality=ResultFinality.CORRECTED,
        home_sets=3,
        away_sets=1,
        home_points=92,
        away_points=88,
    )
    settlements = _line_map(MarketEvaluator().settle(snapshot=snapshot, result=result))
    assert settlements["moneyline-home"].outcome is SettlementOutcome.WIN
    assert settlements["moneyline-away"].outcome is SettlementOutcome.LOSS
    assert settlements["sets-home-minus-1.5"].outcome is SettlementOutcome.WIN
    assert settlements["points-home-minus-5"].outcome is SettlementOutcome.LOSS
    assert settlements["points-away-plus-5"].outcome is SettlementOutcome.WIN
    assert settlements["points-over-180"].outcome is SettlementOutcome.PUSH
    assert settlements["sets-over-4"].outcome is SettlementOutcome.PUSH
    assert {value.result_revision_id for value in settlements.values()} == {"result-revision-7"}
    assert {value.result_revision for value in settlements.values()} == {7}

    void_result = ResultRevision(
        match_id=MATCH_ID,
        result_revision_id="result-revision-8",
        revision=8,
        finality=ResultFinality.VOID,
        home_sets=0,
        away_sets=0,
        home_points=0,
        away_points=0,
    )
    voided = MarketEvaluator().settle(snapshot=snapshot, result=void_result)
    assert {value.outcome for value in voided} == {SettlementOutcome.VOID}


def test_quarter_and_partial_period_contracts_are_explicitly_unsupported() -> None:
    snapshot = _snapshot("unsupported-v1.json")
    availability = MarketAvailability(
        status=AvailabilityStatus.AVAILABLE,
        reason="fixture",
        snapshot=snapshot,
    )
    evaluation = MarketEvaluator().evaluate(_prediction(), availability)
    assert evaluation.eligibility is EvaluationEligibility.UNSUPPORTED
    assert {line.eligibility for line in evaluation.lines} == {EvaluationEligibility.UNSUPPORTED}
    reasons = {line.reason for line in evaluation.lines}
    assert "quarter_or_other_fractional_line_not_supported" in reasons
    assert "partial_period_not_supported" in reasons

    result = ResultRevision(
        match_id=MATCH_ID,
        result_revision_id="result-revision-1",
        revision=1,
        finality=ResultFinality.FINAL,
        home_sets=3,
        away_sets=0,
        home_points=75,
        away_points=50,
    )
    settlements = MarketEvaluator().settle(snapshot=snapshot, result=result)
    assert {settlement.outcome for settlement in settlements} == {SettlementOutcome.UNSUPPORTED}


def test_match_identity_is_required_for_evaluation_and_settlement() -> None:
    snapshot = _snapshot()
    prediction = _prediction()
    object.__setattr__(prediction, "match_id", "different-match")
    with pytest.raises(MarketContractError, match="match IDs differ"):
        MarketEvaluator().evaluate(
            prediction,
            MarketAvailability(
                status=AvailabilityStatus.AVAILABLE,
                reason="fixture",
                snapshot=snapshot,
            ),
        )

    with pytest.raises(MarketContractError, match="match IDs differ"):
        MarketEvaluator().settle(
            snapshot=snapshot,
            result=ResultRevision(
                match_id="different-match",
                result_revision_id="revision-1",
                revision=1,
                finality=ResultFinality.FINAL,
                home_sets=3,
                away_sets=0,
                home_points=75,
                away_points=50,
            ),
        )
