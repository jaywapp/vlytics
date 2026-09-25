"""Immutable Market contracts shared by adapters, evaluators, and repositories."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any

from vlytics.engine.predictors.points import JointScoreDistribution


class MarketType(StrEnum):
    MONEYLINE = "moneyline"
    HANDICAP = "handicap"
    TOTAL = "total"


class MarketUnit(StrEnum):
    MATCH = "match"
    SETS = "sets"
    POINTS = "points"


class MarketPeriod(StrEnum):
    FULL_MATCH = "full_match"
    SET_1 = "set_1"
    SET_2 = "set_2"
    SET_3 = "set_3"
    SET_4 = "set_4"
    SET_5 = "set_5"


class MarketSelection(StrEnum):
    HOME = "home"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    STALE = "stale"
    LATE = "late"


class EvaluationEligibility(StrEnum):
    ELIGIBLE = "eligible"
    MISSING = "missing"
    STALE = "stale"
    LATE = "late"
    UNSUPPORTED = "unsupported"


class SettlementOutcome(StrEnum):
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"
    UNSUPPORTED = "unsupported"


class ResultFinality(StrEnum):
    FINAL = "final"
    CORRECTED = "corrected"
    VOID = "void"


class MarketContractError(ValueError):
    """A Market document violates a structural or semantic invariant."""


@dataclass(frozen=True)
class MarketLine:
    """One two-way quote; handicap signs are added to the selected side."""

    line_id: str
    market_type: MarketType
    unit: MarketUnit
    period: MarketPeriod
    selection: MarketSelection
    line: Decimal | None
    decimal_odds: Decimal
    settlement_rule_version: str
    source: str
    quoted_at: datetime
    observed_at: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_type", MarketType(self.market_type))
        object.__setattr__(self, "unit", MarketUnit(self.unit))
        object.__setattr__(self, "period", MarketPeriod(self.period))
        object.__setattr__(self, "selection", MarketSelection(self.selection))
        if not self.line_id.strip() or not self.source.strip():
            raise MarketContractError("line_id and source must not be blank")
        if not self.settlement_rule_version.strip():
            raise MarketContractError("settlement_rule_version must not be blank")
        odds = _decimal(self.decimal_odds, "decimal_odds")
        object.__setattr__(self, "decimal_odds", odds)
        if odds <= 1:
            raise MarketContractError("decimal_odds must be greater than one")
        if self.line is not None:
            object.__setattr__(self, "line", _decimal(self.line, "line"))
        _validate_times(self.quoted_at, self.observed_at, self.received_at)
        self._validate_shape()

    def _validate_shape(self) -> None:
        if self.market_type is MarketType.MONEYLINE:
            if self.unit is not MarketUnit.MATCH:
                raise MarketContractError("moneyline requires unit=match")
            if self.selection not in {MarketSelection.HOME, MarketSelection.AWAY}:
                raise MarketContractError("moneyline selection must be home or away")
            if self.line is not None:
                raise MarketContractError("moneyline must not have a line")
            return
        if self.line is None:
            raise MarketContractError("handicap and total markets require a line")
        if self.market_type is MarketType.HANDICAP:
            if self.unit not in {MarketUnit.SETS, MarketUnit.POINTS}:
                raise MarketContractError("handicap unit must be sets or points")
            if self.selection not in {MarketSelection.HOME, MarketSelection.AWAY}:
                raise MarketContractError("handicap selection must be home or away")
        elif self.market_type is MarketType.TOTAL:
            if self.unit not in {MarketUnit.SETS, MarketUnit.POINTS}:
                raise MarketContractError("total unit must be sets or points")
            if self.selection not in {MarketSelection.OVER, MarketSelection.UNDER}:
                raise MarketContractError("total selection must be over or under")
            if self.line <= 0:
                raise MarketContractError("total line must be positive")

    @property
    def unsupported_reason(self) -> str | None:
        if self.period is not MarketPeriod.FULL_MATCH:
            return "partial_period_not_supported"
        if self.line is not None and self.line * 2 != (self.line * 2).to_integral_value():
            return "quarter_or_other_fractional_line_not_supported"
        return None

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "line_id": self.line_id,
            "market_type": self.market_type.value,
            "unit": self.unit.value,
            "period": self.period.value,
            "selection": self.selection.value,
            "decimal_odds": _decimal_string(self.decimal_odds),
            "settlement_rule_version": self.settlement_rule_version,
            "source": self.source,
            "quoted_at": self.quoted_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "received_at": self.received_at.isoformat(),
        }
        if self.line is not None:
            document["line"] = _decimal_string(self.line)
        return document


@dataclass(frozen=True)
class MarketSnapshot:
    """One immutable, source-event-mapped collection of contemporaneous quotes."""

    snapshot_id: str
    match_id: str
    source: str
    source_event_id: str
    quoted_at: datetime
    observed_at: datetime
    received_at: datetime
    contract_version: str
    markets: tuple[MarketLine, ...]
    sha256: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.snapshot_id,
                self.match_id,
                self.source,
                self.source_event_id,
                self.contract_version,
            )
        ):
            raise MarketContractError("snapshot identity fields must not be blank")
        _validate_times(self.quoted_at, self.observed_at, self.received_at)
        if not self.markets:
            raise MarketContractError("a Market snapshot must contain at least one line")
        if len({line.line_id for line in self.markets}) != len(self.markets):
            raise MarketContractError("line_id values must be unique within a snapshot")
        for line in self.markets:
            if line.source != self.source:
                raise MarketContractError("line source must match snapshot source")
            if (line.quoted_at, line.observed_at, line.received_at) != (
                self.quoted_at,
                self.observed_at,
                self.received_at,
            ):
                raise MarketContractError("line and snapshot quote timestamps must agree")
        _validate_two_way_pairs(self.markets)
        _require_sha256(self.sha256)
        if self.sha256 != self.canonical_sha256:
            raise MarketContractError("snapshot sha256 does not match its canonical document")

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        match_id: str,
        source: str,
        source_event_id: str,
        quoted_at: datetime,
        observed_at: datetime,
        received_at: datetime,
        markets: tuple[MarketLine, ...],
        contract_version: str = "market-v1",
    ) -> MarketSnapshot:
        digest = _canonical_sha256(
            _snapshot_document(
                snapshot_id=snapshot_id,
                match_id=match_id,
                source=source,
                source_event_id=source_event_id,
                quoted_at=quoted_at,
                observed_at=observed_at,
                received_at=received_at,
                contract_version=contract_version,
                markets=markets,
            )
        )
        return cls(
            snapshot_id=snapshot_id,
            match_id=match_id,
            source=source,
            source_event_id=source_event_id,
            quoted_at=quoted_at,
            observed_at=observed_at,
            received_at=received_at,
            contract_version=contract_version,
            markets=markets,
            sha256=digest,
        )

    @property
    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.to_dict(include_sha256=False))

    def to_dict(self, *, include_sha256: bool = True) -> dict[str, Any]:
        document = _snapshot_document(
            snapshot_id=self.snapshot_id,
            match_id=self.match_id,
            source=self.source,
            source_event_id=self.source_event_id,
            quoted_at=self.quoted_at,
            observed_at=self.observed_at,
            received_at=self.received_at,
            contract_version=self.contract_version,
            markets=self.markets,
        )
        if include_sha256:
            document["sha256"] = self.sha256
        return document


@dataclass(frozen=True)
class MarketAvailability:
    status: AvailabilityStatus
    reason: str
    snapshot: MarketSnapshot | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AvailabilityStatus(self.status))
        if not self.reason.strip():
            raise MarketContractError("availability reason must not be blank")
        if (self.status is AvailabilityStatus.AVAILABLE) != (self.snapshot is not None):
            raise MarketContractError("only available results may contain a snapshot")


@dataclass(frozen=True, init=False)
class PredictionMarketInput:
    """A factory-only view derived from one persistence-owned joint distribution."""

    prediction_id: str
    match_id: str
    input_cutoff_at: datetime
    joint_score_distribution_id: str
    producer_variant_id: str
    input_snapshot_id: str
    home_win_probability: float
    set_score_probabilities: Mapping[str, float]
    point_totals: Mapping[int, float]
    point_differentials: Mapping[int, float]

    def __init__(self) -> None:
        raise TypeError("PredictionMarketInput must be created by a verified factory")

    @classmethod
    def from_joint_score_distribution(
        cls,
        *,
        prediction_id: str,
        match_id: str,
        input_cutoff_at: datetime,
        distribution: JointScoreDistribution,
        producer_variant_id: str,
        input_snapshot_id: str,
    ) -> PredictionMarketInput:
        """Revalidate ownership and derive every marginal from the same joint PMF."""

        if not prediction_id.strip() or not match_id.strip():
            raise MarketContractError("prediction and match IDs must not be blank")
        _require_aware(input_cutoff_at, "input_cutoff_at")
        # Frozen dataclasses can still be tampered with through object.__setattr__ in Python.
        # Re-run the distribution's semantic checks at this trust boundary.
        try:
            distribution.__post_init__()
        except ValueError as error:
            raise MarketContractError("joint score distribution failed revalidation") from error
        source_prediction = distribution.source_set_artifact.prediction
        if source_prediction.match_id != match_id:
            raise MarketContractError("joint score distribution belongs to another match")
        if source_prediction.prediction_cutoff_at != input_cutoff_at:
            raise MarketContractError("prediction cutoff does not match the joint distribution")
        if distribution.producer_variant_id != producer_variant_id:
            raise MarketContractError("joint score distribution belongs to another variant")
        if distribution.input_snapshot_id != input_snapshot_id:
            raise MarketContractError("joint score distribution belongs to another input snapshot")
        instance = object.__new__(cls)
        object.__setattr__(instance, "prediction_id", prediction_id)
        object.__setattr__(instance, "match_id", match_id)
        object.__setattr__(instance, "input_cutoff_at", input_cutoff_at)
        object.__setattr__(instance, "joint_score_distribution_id", distribution.distribution_id)
        object.__setattr__(instance, "producer_variant_id", producer_variant_id)
        object.__setattr__(instance, "input_snapshot_id", input_snapshot_id)
        object.__setattr__(instance, "home_win_probability", distribution.home_win_probability)
        object.__setattr__(
            instance,
            "set_score_probabilities",
            MappingProxyType(dict(distribution.set_score_distribution.as_mapping())),
        )
        object.__setattr__(
            instance,
            "point_totals",
            MappingProxyType(dict(distribution.point_total_distribution)),
        )
        object.__setattr__(
            instance,
            "point_differentials",
            MappingProxyType(dict(distribution.point_differential_distribution)),
        )
        return instance

    @classmethod
    def from_resolver(
        cls,
        *,
        prediction_id: str,
        match_id: str,
        input_cutoff_at: datetime,
        joint_score_distribution_id: str,
        producer_variant_id: str,
        input_snapshot_id: str,
        resolver: Callable[[str], JointScoreDistribution | None],
    ) -> PredictionMarketInput:
        distribution = resolver(joint_score_distribution_id)
        if distribution is None:
            raise MarketContractError("joint score distribution was not found")
        if distribution.distribution_id != joint_score_distribution_id:
            raise MarketContractError("resolved joint score distribution ID is inconsistent")
        return cls.from_joint_score_distribution(
            prediction_id=prediction_id,
            match_id=match_id,
            input_cutoff_at=input_cutoff_at,
            distribution=distribution,
            producer_variant_id=producer_variant_id,
            input_snapshot_id=input_snapshot_id,
        )


@dataclass(frozen=True)
class ResultRevision:
    match_id: str
    result_revision_id: str
    revision: int
    finality: ResultFinality
    home_sets: int
    away_sets: int
    home_points: int
    away_points: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "finality", ResultFinality(self.finality))
        if not self.match_id.strip() or not self.result_revision_id.strip():
            raise MarketContractError("result revision identity must not be blank")
        if self.revision < 1:
            raise MarketContractError("result revision must be positive")
        for name in ("home_sets", "away_sets", "home_points", "away_points"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise MarketContractError("result scores must be non-negative integers")


@dataclass(frozen=True)
class LineProbabilities:
    win: float
    push: float
    loss: float

    def __post_init__(self) -> None:
        values = (self.win, self.push, self.loss)
        if any(not isfinite(value) or value < 0 for value in values):
            raise MarketContractError("line probabilities must be finite and non-negative")
        if abs(sum(values) - 1.0) > 1e-9:
            raise MarketContractError("line probabilities must sum to one")

    def to_dict(self) -> dict[str, float]:
        return {"win": self.win, "push": self.push, "loss": self.loss}


@dataclass(frozen=True)
class MarketLineEvaluation:
    line_id: str
    eligibility: EvaluationEligibility
    reason: str
    probabilities: LineProbabilities | None


@dataclass(frozen=True)
class MarketEvaluation:
    prediction_id: str
    match_id: str
    snapshot_id: str | None
    evaluator_version: str
    eligibility: EvaluationEligibility
    reason: str
    lines: tuple[MarketLineEvaluation, ...]


@dataclass(frozen=True)
class MarketSettlement:
    match_id: str
    snapshot_id: str
    line_id: str
    result_revision_id: str
    result_revision: int
    evaluator_version: str
    outcome: SettlementOutcome
    reason: str


def _validate_two_way_pairs(markets: tuple[MarketLine, ...]) -> None:
    groups: dict[tuple[object, ...], list[MarketLine]] = {}
    for line in markets:
        if line.market_type is MarketType.MONEYLINE:
            key: tuple[object, ...] = (
                line.market_type,
                line.unit,
                line.period,
                line.settlement_rule_version,
            )
        elif line.market_type is MarketType.HANDICAP:
            assert line.line is not None
            key = (
                line.market_type,
                line.unit,
                line.period,
                abs(line.line),
                line.settlement_rule_version,
            )
        else:
            key = (
                line.market_type,
                line.unit,
                line.period,
                line.line,
                line.settlement_rule_version,
            )
        groups.setdefault(key, []).append(line)
    for lines in groups.values():
        selections = {line.selection for line in lines}
        expected = (
            {MarketSelection.OVER, MarketSelection.UNDER}
            if lines[0].market_type is MarketType.TOTAL
            else {MarketSelection.HOME, MarketSelection.AWAY}
        )
        if len(lines) != 2 or selections != expected:
            raise MarketContractError("each quote contract must contain exactly two opposing sides")
        if lines[0].market_type is MarketType.HANDICAP:
            assert lines[0].line is not None and lines[1].line is not None
            if lines[0].line + lines[1].line != 0:
                raise MarketContractError("opposing handicap lines must have inverse signs")


def _snapshot_document(
    *,
    snapshot_id: str,
    match_id: str,
    source: str,
    source_event_id: str,
    quoted_at: datetime,
    observed_at: datetime,
    received_at: datetime,
    contract_version: str,
    markets: tuple[MarketLine, ...],
) -> dict[str, Any]:
    return {
        "schema_version": "market-v1",
        "snapshot_id": snapshot_id,
        "match_id": match_id,
        "source": source,
        "source_event_id": source_event_id,
        "quoted_at": quoted_at.isoformat(),
        "observed_at": observed_at.isoformat(),
        "received_at": received_at.isoformat(),
        "contract_version": contract_version,
        "markets": [line.to_dict() for line in markets],
    }


def _canonical_sha256(document: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def _decimal(value: Decimal | str, name: str) -> Decimal:
    try:
        converted = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise MarketContractError(f"{name} must be a finite decimal") from error
    if not converted.is_finite():
        raise MarketContractError(f"{name} must be a finite decimal")
    return converted


def _decimal_string(value: Decimal) -> str:
    return format(value, "f")


def _validate_times(quoted_at: datetime, observed_at: datetime, received_at: datetime) -> None:
    for name, value in (
        ("quoted_at", quoted_at),
        ("observed_at", observed_at),
        ("received_at", received_at),
    ):
        _require_aware(value, name)
    if quoted_at > observed_at or observed_at > received_at:
        raise MarketContractError("quote timestamps must satisfy quoted <= observed <= received")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketContractError(f"{name} must be timezone-aware")


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise MarketContractError("sha256 must be a lowercase SHA-256 hex digest")


def _probability(value: float, name: str) -> None:
    if not isfinite(value) or not 0 <= value <= 1:
        raise MarketContractError(f"{name} must be finite and between zero and one")


def _probability_distribution(values: Mapping[object, float], name: str) -> None:
    if not values:
        raise MarketContractError(f"{name} must not be empty")
    for value in values.values():
        _probability(value, name)
    if abs(sum(values.values()) - 1.0) > 1e-9:
        raise MarketContractError(f"{name} probability mass must sum to one")
