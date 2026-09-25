"""Post-prediction Market comparison and result-revision settlement."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from vlytics.engine.market.models import (
    AvailabilityStatus,
    EvaluationEligibility,
    LineProbabilities,
    MarketAvailability,
    MarketContractError,
    MarketEvaluation,
    MarketLine,
    MarketLineEvaluation,
    MarketSelection,
    MarketSettlement,
    MarketSnapshot,
    MarketType,
    MarketUnit,
    PredictionMarketInput,
    ResultFinality,
    ResultRevision,
    SettlementOutcome,
)

EVALUATOR_VERSION = "market-evaluator-v1"


class MarketEvaluator:
    def __init__(self, evaluator_version: str = EVALUATOR_VERSION) -> None:
        if not evaluator_version.strip():
            raise ValueError("evaluator_version must not be blank")
        self.evaluator_version = evaluator_version

    def evaluate(
        self,
        prediction: PredictionMarketInput,
        availability: MarketAvailability,
    ) -> MarketEvaluation:
        if availability.status is AvailabilityStatus.MISSING:
            return MarketEvaluation(
                prediction_id=prediction.prediction_id,
                match_id=prediction.match_id,
                snapshot_id=None,
                evaluator_version=self.evaluator_version,
                eligibility=EvaluationEligibility.MISSING,
                reason=availability.reason,
                lines=(),
            )
        if availability.status is AvailabilityStatus.LATE:
            return MarketEvaluation(
                prediction_id=prediction.prediction_id,
                match_id=prediction.match_id,
                snapshot_id=None,
                evaluator_version=self.evaluator_version,
                eligibility=EvaluationEligibility.LATE,
                reason=availability.reason,
                lines=(),
            )
        if availability.status is AvailabilityStatus.STALE:
            return MarketEvaluation(
                prediction_id=prediction.prediction_id,
                match_id=prediction.match_id,
                snapshot_id=None,
                evaluator_version=self.evaluator_version,
                eligibility=EvaluationEligibility.STALE,
                reason=availability.reason,
                lines=(),
            )
        snapshot = availability.snapshot
        assert snapshot is not None
        if snapshot.match_id != prediction.match_id:
            raise MarketContractError("prediction and Market snapshot match IDs differ")
        if (
            snapshot.quoted_at > prediction.input_cutoff_at
            or snapshot.observed_at > prediction.input_cutoff_at
            or snapshot.received_at > prediction.input_cutoff_at
        ):
            return MarketEvaluation(
                prediction_id=prediction.prediction_id,
                match_id=prediction.match_id,
                snapshot_id=None,
                evaluator_version=self.evaluator_version,
                eligibility=EvaluationEligibility.MISSING,
                reason="market_snapshot_not_available_at_prediction_cutoff",
                lines=(),
            )
        lines = tuple(self._evaluate_line(prediction, line) for line in snapshot.markets)
        eligibility = (
            EvaluationEligibility.ELIGIBLE
            if any(line.eligibility is EvaluationEligibility.ELIGIBLE for line in lines)
            else EvaluationEligibility.UNSUPPORTED
        )
        return MarketEvaluation(
            prediction_id=prediction.prediction_id,
            match_id=prediction.match_id,
            snapshot_id=snapshot.snapshot_id,
            evaluator_version=self.evaluator_version,
            eligibility=eligibility,
            reason=(
                "at_least_one_supported_market_line"
                if eligibility is EvaluationEligibility.ELIGIBLE
                else "no_supported_market_lines"
            ),
            lines=lines,
        )

    def settle(
        self,
        *,
        snapshot: MarketSnapshot,
        result: ResultRevision,
    ) -> tuple[MarketSettlement, ...]:
        if snapshot.match_id != result.match_id:
            raise MarketContractError("result and Market snapshot match IDs differ")
        return tuple(self._settle_line(snapshot, line, result) for line in snapshot.markets)

    def _evaluate_line(
        self,
        prediction: PredictionMarketInput,
        line: MarketLine,
    ) -> MarketLineEvaluation:
        if line.unsupported_reason is not None:
            return _unsupported_evaluation(line, line.unsupported_reason)
        if line.market_type is MarketType.MONEYLINE:
            if prediction.home_win_probability is None:
                return _unsupported_evaluation(line, "winner_distribution_unavailable")
            home = prediction.home_win_probability
            win = home if line.selection is MarketSelection.HOME else 1.0 - home
            probabilities = LineProbabilities(win=win, push=0.0, loss=1.0 - win)
        elif line.unit is MarketUnit.SETS:
            if prediction.set_score_probabilities is None:
                return _unsupported_evaluation(line, "set_score_distribution_unavailable")
            values = _set_values(prediction.set_score_probabilities, line)
            probabilities = _line_probabilities(values)
        elif line.unit is MarketUnit.POINTS:
            distribution = (
                prediction.point_differentials
                if line.market_type is MarketType.HANDICAP
                else prediction.point_totals
            )
            if distribution is None:
                return _unsupported_evaluation(line, "joint_score_distribution_unavailable")
            values = _point_values(distribution, line)
            probabilities = _line_probabilities(values)
        else:
            return _unsupported_evaluation(line, "market_unit_not_supported")
        return MarketLineEvaluation(
            line_id=line.line_id,
            eligibility=EvaluationEligibility.ELIGIBLE,
            reason="probabilities_derived_after_prediction",
            probabilities=probabilities,
        )

    def _settle_line(
        self,
        snapshot: MarketSnapshot,
        line: MarketLine,
        result: ResultRevision,
    ) -> MarketSettlement:
        if result.finality is ResultFinality.VOID:
            outcome = SettlementOutcome.VOID
            reason = "result_revision_void"
        elif line.unsupported_reason is not None:
            outcome = SettlementOutcome.UNSUPPORTED
            reason = line.unsupported_reason
        else:
            comparison = _result_comparison(line, result)
            if comparison > 0:
                outcome = SettlementOutcome.WIN
            elif comparison < 0:
                outcome = SettlementOutcome.LOSS
            else:
                outcome = SettlementOutcome.PUSH
            reason = "settled_against_exact_result_revision"
        return MarketSettlement(
            match_id=result.match_id,
            snapshot_id=snapshot.snapshot_id,
            line_id=line.line_id,
            result_revision_id=result.result_revision_id,
            result_revision=result.revision,
            evaluator_version=self.evaluator_version,
            outcome=outcome,
            reason=reason,
        )


def _unsupported_evaluation(line: MarketLine, reason: str) -> MarketLineEvaluation:
    return MarketLineEvaluation(
        line_id=line.line_id,
        eligibility=EvaluationEligibility.UNSUPPORTED,
        reason=reason,
        probabilities=None,
    )


def _set_values(distribution: Mapping[str, float], line: MarketLine) -> dict[Decimal, float]:
    assert line.line is not None
    values: dict[Decimal, float] = {}
    for outcome, probability in distribution.items():
        home_sets, away_sets = (Decimal(value) for value in outcome.split(":"))
        if line.market_type is MarketType.HANDICAP:
            difference = home_sets - away_sets
            selected = difference if line.selection is MarketSelection.HOME else -difference
            comparison = selected + line.line
        else:
            total = home_sets + away_sets
            comparison = total - line.line
            if line.selection is MarketSelection.UNDER:
                comparison = -comparison
        values[comparison] = values.get(comparison, 0.0) + probability
    return values


def _point_values(distribution: Mapping[int, float], line: MarketLine) -> dict[Decimal, float]:
    assert line.line is not None
    values: dict[Decimal, float] = {}
    for raw_value, probability in distribution.items():
        value = Decimal(raw_value)
        if line.market_type is MarketType.HANDICAP:
            selected = value if line.selection is MarketSelection.HOME else -value
            comparison = selected + line.line
        else:
            comparison = value - line.line
            if line.selection is MarketSelection.UNDER:
                comparison = -comparison
        values[comparison] = values.get(comparison, 0.0) + probability
    return values


def _line_probabilities(values: Mapping[Decimal, float]) -> LineProbabilities:
    return LineProbabilities(
        win=sum(probability for value, probability in values.items() if value > 0),
        push=sum(probability for value, probability in values.items() if value == 0),
        loss=sum(probability for value, probability in values.items() if value < 0),
    )


def _result_comparison(line: MarketLine, result: ResultRevision) -> Decimal:
    if line.market_type is MarketType.MONEYLINE:
        home_won = result.home_sets > result.away_sets
        selected_won = home_won if line.selection is MarketSelection.HOME else not home_won
        return Decimal(1 if selected_won else -1)
    assert line.line is not None
    if line.unit is MarketUnit.SETS:
        home_value = result.home_sets
        away_value = result.away_sets
    elif line.unit is MarketUnit.POINTS:
        home_value = result.home_points
        away_value = result.away_points
    else:
        raise MarketContractError("unsupported settlement unit")
    if line.market_type is MarketType.HANDICAP:
        difference = Decimal(home_value - away_value)
        selected = difference if line.selection is MarketSelection.HOME else -difference
        return selected + line.line
    total = Decimal(home_value + away_value)
    comparison = total - line.line
    return -comparison if line.selection is MarketSelection.UNDER else comparison
