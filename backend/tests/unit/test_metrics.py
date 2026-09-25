"""Hand-calculated result evaluation and fair-cohort aggregation tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from math import log
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from vlytics.engine.evaluation import (
    CohortKey,
    ComparisonSpec,
    EvaluationRepository,
    PerformanceObservation,
    PredictionEvaluationInput,
    ResultEvaluator,
    ResultRevision,
    aggregate_performance,
    binary_accuracy,
    binary_brier,
    binary_log_loss,
    calibration_bins,
    market_accuracy,
    paired_difference,
    set_ranked_probability_score,
    set_score_accuracy,
)
from vlytics.engine.market import (
    EVALUATOR_VERSION as MARKET_EVALUATOR_VERSION,
)
from vlytics.engine.market import (
    EvaluationEligibility,
    MarketEvaluation,
    MarketValidationContext,
    validate_market_snapshot_v1,
)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2030, 1, 1, tzinfo=UTC)
MATCH_ID = "11111111-1111-1111-1111-111111111111"
SCHEDULE_ID = "22222222-2222-2222-2222-222222222222"
SNAPSHOT_ID = "33333333-3333-3333-3333-333333333333"


def _cohort(
    provider: str = "gpt",
    model_version: str = "model-v1",
    *,
    division: str = "men",
    stage: str = "regular",
    availability_policy: str = "live_prospective",
    timing_eligibility: str = "on_time",
    result_finality: str = "final",
) -> CohortKey:
    return CohortKey(
        division=division,
        competition="kovo",
        stage=stage,
        provider=provider,
        model_version=model_version,
        prompt_version="prompt-v1" if provider != "statistical" else "not-applicable",
        feature_version="feature-v1",
        availability_policy=availability_policy,
        timing_eligibility=timing_eligibility,
        result_finality=result_finality,
    )


def _prediction(
    probability: float,
    *,
    prediction_id: str = "44444444-4444-4444-4444-444444444444",
    cohort: CohortKey | None = None,
    sets: dict[str, float] | None = None,
) -> PredictionEvaluationInput:
    return PredictionEvaluationInput(
        prediction_id=prediction_id,
        match_id=MATCH_ID,
        schedule_revision_id=SCHEDULE_ID,
        snapshot_id=SNAPSHOT_ID,
        input_cutoff_at=NOW,
        cohort=cohort or _cohort(),
        home_win_probability=probability,
        set_score_probabilities=sets,
    )


def _result(
    *,
    result_id: str = "55555555-5555-5555-5555-555555555555",
    revision: int = 1,
    finality: str = "final",
    score: tuple[int, int] = (3, 0),
    points: tuple[int, int] = (75, 50),
) -> ResultRevision:
    return ResultRevision(
        result_revision_id=result_id,
        match_id=MATCH_ID,
        revision=revision,
        finality=finality,
        home_sets=score[0],
        away_sets=score[1],
        home_points=points[0],
        away_points=points[1],
    )


def _observation(evaluation: Any) -> PerformanceObservation:
    return PerformanceObservation(
        match_id=evaluation.prediction.match_id,
        pairing_key=evaluation.pairing_key,
        cohort=evaluation.prediction.cohort,
        prediction_available=True,
        result_available=True,
        evaluation=evaluation,
    )


def test_binary_metrics_are_hand_calculated_and_clip_only_log_loss() -> None:
    assert binary_brier(0.8, 1) == pytest.approx(0.04)
    assert binary_log_loss(0.8, 1) == pytest.approx(-log(0.8))
    assert binary_accuracy(0.5, 1) == 1.0
    assert binary_accuracy(0.5, 0) == 0.0
    assert binary_brier(0.0, 1) == 1.0
    assert binary_brier(1.0, 0) == 1.0
    assert binary_log_loss(0.0, 1) == pytest.approx(-log(1e-12))
    assert binary_log_loss(1.0, 0) == pytest.approx(-log(1e-12))


def test_set_rps_and_exact_accuracy_are_hand_calculated() -> None:
    perfect = {score: float(score == "3:0") for score in ("3:0", "3:1", "3:2", "2:3", "1:3", "0:3")}
    opposite = {score: float(score == "0:3") for score in perfect}
    assert set_ranked_probability_score(perfect, "3:0") == 0.0
    assert set_ranked_probability_score(opposite, "3:0") == 1.0

    tied = {score: 0.0 for score in perfect}
    tied["3:0"] = 0.5
    tied["3:1"] = 0.5
    assert set_score_accuracy(tied, "3:0") == 0.5
    assert set_score_accuracy(tied, "3:2") == 0.0


def test_calibration_has_fixed_bins_and_wilson_uncertainty() -> None:
    bins = calibration_bins([(0.0, 0), (0.2, 1), (0.8, 1), (1.0, 1)])
    assert [(item.lower_bound, item.sample_size) for item in bins] == [
        (0.0, 1),
        (0.2, 1),
        (0.8, 2),
    ]
    assert bins[-1].mean_probability == pytest.approx(0.9)
    assert bins[-1].observed_rate == 1.0
    assert bins[-1].uncertainty_lower == pytest.approx(0.3423802275)
    assert bins[-1].uncertainty_upper == 1.0
    assert calibration_bins([]) == ()


def test_paired_difference_handles_n_zero_one_and_sample_standard_error() -> None:
    empty = paired_difference([], metric="brier")
    assert empty.sample_size == 0
    assert empty.mean_difference is None
    assert empty.standard_error is None

    one = paired_difference([-0.1], metric="brier")
    assert one.mean_difference == -0.1
    assert one.standard_error is None
    assert one.confidence_lower is None

    summary = paired_difference([-0.1, 0.1], metric="brier")
    assert summary.mean_difference == 0.0
    assert summary.standard_error == pytest.approx(0.1)
    assert summary.confidence_lower == pytest.approx(-1.959963984540054 * 0.1)
    assert summary.confidence_upper == pytest.approx(1.959963984540054 * 0.1)


def test_market_accuracy_excludes_push_void_and_unavailable_states() -> None:
    summary = market_accuracy(
        ["win", "loss", "push", "void", "missing", "stale", "late", "unsupported"]
    )
    assert summary.denominator == 2
    assert summary.accuracy == 0.5
    assert summary.counts["push"] == 1
    assert summary.counts["void"] == 1
    no_decisions = market_accuracy(["push", "void", "missing"])
    assert no_decisions.denominator == 0
    assert no_decisions.accuracy is None


def test_result_evaluator_uses_exact_revision_and_preserves_missing_market_metrics() -> None:
    evaluator = ResultEvaluator()
    missing_market = MarketEvaluation(
        prediction_id=_prediction(0.8).prediction_id,
        match_id=MATCH_ID,
        snapshot_id=None,
        evaluator_version=MARKET_EVALUATOR_VERSION,
        eligibility=EvaluationEligibility.MISSING,
        reason="no_quote",
        lines=(),
    )
    evaluation = evaluator.evaluate(
        _prediction(0.8),
        _result(),
        market_evaluation=missing_market,
    )
    assert evaluation.eligible is True
    assert evaluation.brier == pytest.approx(0.04)
    assert evaluation.market_status == "missing"
    assert evaluation.market_settlements == ()

    corrected = evaluator.evaluate(
        _prediction(0.8),
        _result(
            result_id="66666666-6666-6666-6666-666666666666",
            revision=2,
            finality="corrected",
            score=(0, 3),
        ),
    )
    assert evaluation.result.result_revision_id != corrected.result.result_revision_id
    assert evaluation.brier == pytest.approx(0.04)
    assert corrected.brier == pytest.approx(0.64)
    assert corrected.prediction.cohort.result_finality == "corrected"


def test_market_settlement_is_created_only_for_eligible_snapshot() -> None:
    document = json.loads(
        (ROOT / "fixtures" / "synthetic" / "market" / "full-match-v1.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads((ROOT / "contracts" / "market-v1.schema.json").read_text(encoding="utf-8"))
    snapshot = validate_market_snapshot_v1(
        document,
        schema,
        context=MarketValidationContext(match_id=document["match_id"]),
    )
    prediction = replace(
        _prediction(0.8),
        prediction_id="prediction-market-1",
        match_id=document["match_id"],
    )
    result = replace(
        _result(),
        match_id=document["match_id"],
    )
    eligible = MarketEvaluation(
        prediction_id=prediction.prediction_id,
        match_id=prediction.match_id,
        snapshot_id=snapshot.snapshot_id,
        evaluator_version=MARKET_EVALUATOR_VERSION,
        eligibility=EvaluationEligibility.ELIGIBLE,
        reason="fixture",
        lines=(),
    )
    evaluation = ResultEvaluator().evaluate(
        prediction,
        result,
        market_evaluation=eligible,
        market_snapshot=snapshot,
    )
    assert evaluation.market_status == "eligible"
    assert len(evaluation.market_settlements) == len(snapshot.markets)
    assert {item.result_revision_id for item in evaluation.market_settlements} == {
        result.result_revision_id
    }

    provisional = ResultEvaluator().evaluate(
        prediction,
        replace(result, finality="provisional"),
        market_evaluation=eligible,
        market_snapshot=snapshot,
    )
    assert provisional.eligible is True
    assert provisional.market_status == "eligible"
    assert provisional.market_settlements == ()

    ineligible = replace(
        eligible,
        snapshot_id=None,
        eligibility=EvaluationEligibility.STALE,
    )
    with pytest.raises(ValueError, match="ineligible Market evaluation"):
        ResultEvaluator().evaluate(
            prediction,
            result,
            market_evaluation=ineligible,
            market_snapshot=snapshot,
        )


def test_aggregation_keeps_individual_and_paired_n_and_missing_market_separate() -> None:
    evaluator = ResultEvaluator()
    ai_cohort = _cohort("gpt", "gpt-v1")
    baseline_cohort = _cohort("statistical", "elo-v1")
    first_ai = evaluator.evaluate(_prediction(0.8, cohort=ai_cohort), _result())
    first_baseline = evaluator.evaluate(
        _prediction(
            0.6,
            prediction_id="77777777-7777-7777-7777-777777777777",
            cohort=baseline_cohort,
        ),
        _result(),
    )
    second_prediction = replace(
        _prediction(
            0.3,
            prediction_id="88888888-8888-8888-8888-888888888888",
            cohort=ai_cohort,
        ),
        match_id="99999999-9999-9999-9999-999999999999",
        schedule_revision_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        snapshot_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    second_result = replace(
        _result(),
        match_id=second_prediction.match_id,
        result_revision_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        home_sets=0,
        away_sets=3,
    )
    second_ai = evaluator.evaluate(second_prediction, second_result)
    report = aggregate_performance(
        [_observation(first_ai), _observation(first_baseline), _observation(second_ai)],
        generated_at=NOW,
        comparisons=[ComparisonSpec("gpt-minus-elo", ai_cohort, baseline_cohort)],
    )
    comparison = report.comparisons[0]
    assert comparison.ai_individual_n == 2
    assert comparison.baseline_individual_n == 1
    assert comparison.paired_n == 1
    assert comparison.brier.mean_difference == pytest.approx(0.04 - 0.16)
    assert comparison.brier.standard_error is None
    assert comparison.excluded_reasons == {"baseline_unavailable": 1}


def test_aggregation_reports_n_zero_failures_and_separates_all_cohort_dimensions() -> None:
    cohorts = [
        _cohort(division="men"),
        _cohort(division="women"),
        _cohort(stage="playoff"),
        _cohort(model_version="model-v2"),
        replace(_cohort(), prompt_version="prompt-v2"),
        replace(_cohort(), feature_version="feature-v2"),
        _cohort(
            availability_policy="historical_reconstruction", timing_eligibility="reconstructed"
        ),
        _cohort(timing_eligibility="diagnostic"),
        _cohort(result_finality="corrected"),
    ]
    observations = [
        PerformanceObservation(
            match_id=f"match-{index}",
            pairing_key=(f"match-{index}", f"schedule-{index}", f"snapshot-{index}", NOW),
            cohort=cohort,
            prediction_available=False,
            result_available=index % 2 == 0,
            failure_reason="provider_timeout",
        )
        for index, cohort in enumerate(cohorts)
    ]
    report = aggregate_performance(observations, generated_at=NOW)
    assert len(report.cohorts) == len(cohorts)
    assert all(item.metrics.winner_n == 0 for item in report.cohorts)
    assert all(item.metrics.brier is None for item in report.cohorts)
    assert all(item.coverage.coverage == 0 for item in report.cohorts)
    assert all(item.coverage.reasons["provider_timeout"] == 1 for item in report.cohorts)


def test_aggregation_preserves_missing_evaluations_and_both_unavailable_pairs() -> None:
    ai_cohort = _cohort("gpt", "gpt-v1")
    baseline_cohort = _cohort("statistical", "elo-v1")
    pairing_key = (MATCH_ID, SCHEDULE_ID, SNAPSHOT_ID, NOW)
    report = aggregate_performance(
        [
            PerformanceObservation(
                match_id=MATCH_ID,
                pairing_key=pairing_key,
                cohort=ai_cohort,
                prediction_available=False,
                result_available=True,
                failure_reason="provider_timeout",
            ),
            PerformanceObservation(
                match_id=MATCH_ID,
                pairing_key=pairing_key,
                cohort=baseline_cohort,
                prediction_available=True,
                result_available=True,
            ),
        ],
        generated_at=NOW,
        comparisons=[ComparisonSpec("gpt-minus-elo", ai_cohort, baseline_cohort)],
    )

    reports = {item.cohort: item for item in report.cohorts}
    assert reports[baseline_cohort].coverage.reasons == {"evaluation_missing": 1}
    comparison = report.comparisons[0]
    assert comparison.ai_individual_n == 0
    assert comparison.baseline_individual_n == 0
    assert comparison.paired_n == 0
    assert comparison.excluded_reasons == {"both_unavailable": 1}


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def one(self) -> object:
        return self.value


class _Connection:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.parameters: list[dict[str, object]] = []

    def execute(self, _statement: object, parameters: dict[str, object]) -> _Result:
        self.parameters.append(parameters)
        if not self.results:
            raise AssertionError("unexpected repository query")
        return _Result(self.results.pop(0))


def test_evaluation_repository_is_idempotent_and_corrections_append() -> None:
    evaluator = ResultEvaluator()
    original = evaluator.evaluate(_prediction(0.8), _result())
    original_id = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    original_metrics = original.metric_values()
    original_settlement = original.settlement_values()
    rerun_connection = _Connection(
        original_id,
        None,
        (
            original_id,
            UUID(MATCH_ID),
            original_metrics,
            original_settlement,
        ),
    )
    repository = EvaluationRepository(rerun_connection)  # type: ignore[arg-type]
    assert repository.add(original).created is True
    rerun = repository.add(original)
    assert rerun.id == original_id
    assert rerun.created is False

    corrected = evaluator.evaluate(
        _prediction(0.8),
        _result(
            result_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            revision=2,
            finality="corrected",
            score=(0, 3),
        ),
    )
    corrected_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    append_connection = _Connection(original_id, corrected_id)
    append_repository = EvaluationRepository(append_connection)  # type: ignore[arg-type]
    append_repository.add(original)
    append_repository.add(corrected)
    assert append_connection.parameters[0]["result_revision_id"] == UUID(
        original.result.result_revision_id
    )
    assert append_connection.parameters[1]["result_revision_id"] == UUID(
        corrected.result.result_revision_id
    )
    assert original.brier != corrected.brier


def test_performance_report_matches_schema() -> None:
    evaluation = ResultEvaluator().evaluate(_prediction(0.75), _result())
    report = aggregate_performance([_observation(evaluation)], generated_at=NOW)
    schema = json.loads(
        (ROOT / "contracts" / "performance-v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(report.to_dict())
    assert report.cohorts[0].metrics.winner_n == 1
    assert report.cohorts[0].coverage.evaluated == 1
    assert sum(item.sample_size for item in report.cohorts[0].calibration) == 1
