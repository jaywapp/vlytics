"""JSON Schema and semantic validation for MarketSnapshotV1."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from vlytics.engine.market.models import (
    MarketContractError,
    MarketLine,
    MarketPeriod,
    MarketSelection,
    MarketSnapshot,
    MarketType,
    MarketUnit,
)

SourceEventResolver = Callable[[str, str], str | None]
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


@dataclass(frozen=True)
class MarketValidationContext:
    """Persistence-owned match mapping expected for an external source event."""

    match_id: str | None = None
    source_event_resolver: SourceEventResolver | None = None


def validate_market_snapshot_v1(
    document: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    context: MarketValidationContext | None = None,
) -> MarketSnapshot:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "$"
        raise MarketContractError(f"market schema violation at {location}: {first.message}")
    snapshot = _parse_snapshot(document)
    expected = context or MarketValidationContext()
    if expected.match_id is not None and snapshot.match_id != expected.match_id:
        raise MarketContractError("snapshot match_id does not match the requested match")
    if expected.source_event_resolver is not None:
        mapped_match = expected.source_event_resolver(snapshot.source, snapshot.source_event_id)
        if mapped_match is None:
            raise MarketContractError("source_event_id has no explicit match mapping")
        if mapped_match != snapshot.match_id:
            raise MarketContractError("source_event mapping points to another match")
    return snapshot


def _parse_snapshot(document: Mapping[str, Any]) -> MarketSnapshot:
    markets_value = document["markets"]
    if not isinstance(markets_value, Sequence) or isinstance(markets_value, str | bytes):
        raise MarketContractError("markets must be an array")
    markets = tuple(_parse_line(_mapping(item, "market line")) for item in markets_value)
    return MarketSnapshot(
        snapshot_id=_string(document, "snapshot_id"),
        match_id=_string(document, "match_id"),
        source=_string(document, "source"),
        source_event_id=_string(document, "source_event_id"),
        quoted_at=_datetime(document, "quoted_at"),
        observed_at=_datetime(document, "observed_at"),
        received_at=_datetime(document, "received_at"),
        contract_version=_string(document, "contract_version"),
        markets=markets,
        sha256=_string(document, "sha256"),
    )


def _parse_line(document: Mapping[str, Any]) -> MarketLine:
    line_value = document.get("line")
    return MarketLine(
        line_id=_string(document, "line_id"),
        market_type=MarketType(_string(document, "market_type")),
        unit=MarketUnit(_string(document, "unit")),
        period=MarketPeriod(_string(document, "period")),
        selection=MarketSelection(_string(document, "selection")),
        line=Decimal(line_value) if isinstance(line_value, str) else None,
        decimal_odds=Decimal(_string(document, "decimal_odds")),
        settlement_rule_version=_string(document, "settlement_rule_version"),
        source=_string(document, "source"),
        quoted_at=_datetime(document, "quoted_at"),
        observed_at=_datetime(document, "observed_at"),
        received_at=_datetime(document, "received_at"),
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MarketContractError(f"{name} must be an object")
    return value


def _string(document: Mapping[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise MarketContractError(f"{name} must be a non-blank string")
    return value


def _datetime(document: Mapping[str, Any], name: str) -> datetime:
    raw = _string(document, name)
    if _RFC3339.fullmatch(raw) is None:
        raise MarketContractError(f"{name} must be an RFC 3339 date-time")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise MarketContractError(f"{name} must be an ISO 8601 datetime") from error
