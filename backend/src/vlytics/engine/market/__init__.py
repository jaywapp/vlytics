"""Versioned Market contracts and post-prediction evaluation."""

from vlytics.engine.market.adapters import (
    MarketAdapter,
    MissingMarketAdapter,
    StoredMarketAdapter,
    SyntheticMarketAdapter,
    default_market_adapter,
)
from vlytics.engine.market.evaluator import EVALUATOR_VERSION, MarketEvaluator
from vlytics.engine.market.models import (
    AvailabilityStatus,
    EvaluationEligibility,
    LineProbabilities,
    MarketAvailability,
    MarketContractError,
    MarketEvaluation,
    MarketLine,
    MarketLineEvaluation,
    MarketPeriod,
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
from vlytics.engine.market.repository import (
    MarketEvaluationRepository,
    MarketSnapshotRepository,
)
from vlytics.engine.market.validation import (
    MarketValidationContext,
    validate_market_snapshot_v1,
)

__all__ = [
    "EVALUATOR_VERSION",
    "AvailabilityStatus",
    "EvaluationEligibility",
    "LineProbabilities",
    "MarketAdapter",
    "MarketAvailability",
    "MarketContractError",
    "MarketEvaluation",
    "MarketEvaluationRepository",
    "MarketEvaluator",
    "MarketLine",
    "MarketLineEvaluation",
    "MarketPeriod",
    "MarketSelection",
    "MarketSettlement",
    "MarketSnapshot",
    "MarketSnapshotRepository",
    "MarketType",
    "MarketUnit",
    "MarketValidationContext",
    "MissingMarketAdapter",
    "PredictionMarketInput",
    "ResultFinality",
    "ResultRevision",
    "SettlementOutcome",
    "StoredMarketAdapter",
    "SyntheticMarketAdapter",
    "default_market_adapter",
    "validate_market_snapshot_v1",
]
