"""Result-revision evaluator with optional, eligibility-gated Market settlement."""

from __future__ import annotations

from dataclasses import replace

from vlytics.engine.evaluation.metrics import (
    binary_accuracy,
    binary_brier,
    binary_log_loss,
    set_ranked_probability_score,
    set_score_accuracy,
)
from vlytics.engine.evaluation.models import (
    COHORT_POLICY_VERSION,
    EVALUATOR_VERSION,
    Evaluation,
    PredictionEvaluationInput,
    ResultRevision,
)
from vlytics.engine.market import (
    EvaluationEligibility,
    MarketEvaluation,
    MarketEvaluator,
    MarketSettlement,
    MarketSnapshot,
)
from vlytics.engine.market import (
    ResultFinality as MarketResultFinality,
)
from vlytics.engine.market import (
    ResultRevision as MarketResultRevision,
)


class ResultEvaluator:
    """Evaluate one immutable prediction against one exact result revision."""

    def __init__(
        self,
        *,
        evaluator_version: str = EVALUATOR_VERSION,
        cohort_policy_version: str = COHORT_POLICY_VERSION,
        market_evaluator: MarketEvaluator | None = None,
    ) -> None:
        if not evaluator_version.strip() or not cohort_policy_version.strip():
            raise ValueError("evaluation versions must not be blank")
        self.evaluator_version = evaluator_version
        self.cohort_policy_version = cohort_policy_version
        self.market_evaluator = market_evaluator or MarketEvaluator()

    def evaluate(
        self,
        prediction: PredictionEvaluationInput,
        result: ResultRevision,
        *,
        market_evaluation: MarketEvaluation | None = None,
        market_snapshot: MarketSnapshot | None = None,
    ) -> Evaluation:
        if prediction.match_id != result.match_id:
            raise ValueError("prediction and result match IDs differ")
        if prediction.cohort.result_finality != result.finality:
            prediction = replace(
                prediction,
                cohort=replace(prediction.cohort, result_finality=result.finality),
            )

        market_status, settlements = self._market_settlement(
            prediction,
            result,
            market_evaluation,
            market_snapshot,
        )
        if result.finality == "void":
            return Evaluation(
                prediction=prediction,
                result=result,
                evaluator_version=self.evaluator_version,
                cohort_policy_version=self.cohort_policy_version,
                eligible=False,
                reason="result_revision_void",
                brier=None,
                log_loss=None,
                winner_accuracy=None,
                set_rps=None,
                set_score_accuracy=None,
                market_status=market_status,
                market_settlements=settlements,
            )
        if prediction.home_win_probability is None:
            return Evaluation(
                prediction=prediction,
                result=result,
                evaluator_version=self.evaluator_version,
                cohort_policy_version=self.cohort_policy_version,
                eligible=False,
                reason="winner_probability_unavailable",
                brier=None,
                log_loss=None,
                winner_accuracy=None,
                set_rps=None,
                set_score_accuracy=None,
                market_status=market_status,
                market_settlements=settlements,
            )

        outcome = result.home_won
        probability = prediction.home_win_probability
        set_rps = None
        exact_accuracy = None
        if prediction.set_score_probabilities is not None:
            set_rps = set_ranked_probability_score(
                prediction.set_score_probabilities,
                result.set_score,
            )
            exact_accuracy = set_score_accuracy(
                prediction.set_score_probabilities,
                result.set_score,
            )
        return Evaluation(
            prediction=prediction,
            result=result,
            evaluator_version=self.evaluator_version,
            cohort_policy_version=self.cohort_policy_version,
            eligible=True,
            reason="evaluated_against_exact_result_revision",
            brier=binary_brier(probability, outcome),
            log_loss=binary_log_loss(probability, outcome),
            winner_accuracy=binary_accuracy(probability, outcome),
            set_rps=set_rps,
            set_score_accuracy=exact_accuracy,
            market_status=market_status,
            market_settlements=settlements,
        )

    def _market_settlement(
        self,
        prediction: PredictionEvaluationInput,
        result: ResultRevision,
        market_evaluation: MarketEvaluation | None,
        market_snapshot: MarketSnapshot | None,
    ) -> tuple[str, tuple[MarketSettlement, ...]]:
        if market_evaluation is None:
            if market_snapshot is not None:
                raise ValueError("market_snapshot requires its eligibility evaluation")
            return "not_requested", ()
        if market_evaluation.prediction_id != prediction.prediction_id:
            raise ValueError("prediction and Market evaluation IDs differ")
        if market_evaluation.match_id != prediction.match_id:
            raise ValueError("prediction and Market evaluation match IDs differ")
        status = market_evaluation.eligibility.value
        if market_evaluation.eligibility is not EvaluationEligibility.ELIGIBLE:
            if market_snapshot is not None:
                raise ValueError("ineligible Market evaluation must not be settled")
            return status, ()
        if market_snapshot is None:
            raise ValueError("eligible Market evaluation requires its exact snapshot")
        if market_evaluation.snapshot_id != market_snapshot.snapshot_id:
            raise ValueError("Market evaluation and snapshot IDs differ")
        if market_evaluation.evaluator_version != self.market_evaluator.evaluator_version:
            raise ValueError("Market evaluation and settlement versions differ")
        if result.finality == "provisional":
            return "eligible", ()
        market_result = MarketResultRevision(
            match_id=result.match_id,
            result_revision_id=result.result_revision_id,
            revision=result.revision,
            finality=MarketResultFinality(result.finality),
            home_sets=result.home_sets,
            away_sets=result.away_sets,
            home_points=result.home_points,
            away_points=result.away_points,
        )
        return "eligible", self.market_evaluator.settle(
            snapshot=market_snapshot,
            result=market_result,
        )
