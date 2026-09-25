"""Persisted Market adapter as-of state tests without a live database."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from vlytics.engine.market import (
    AvailabilityStatus,
    EvaluationEligibility,
    MarketContractError,
    MarketEvaluation,
    MarketEvaluationRepository,
    MarketSnapshotRepository,
    StoredMarketAdapter,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads(
    (REPOSITORY_ROOT / "contracts" / "market-v1.schema.json").read_text(encoding="utf-8")
)
DOCUMENT = json.loads(
    (REPOSITORY_ROOT / "fixtures" / "synthetic" / "market" / "full-match-v1.json").read_text(
        encoding="utf-8"
    )
)
MATCH_ID = UUID(DOCUMENT["match_id"])


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value

    def scalar_one_or_none(self) -> object:
        return self.value

    def one(self) -> object:
        return self.value


class _Connection:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.executed: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: object, parameters: dict[str, object] | None = None) -> _Result:
        self.executed.append((str(statement), parameters or {}))
        if not self.results:
            raise AssertionError("unexpected repository query")
        return _Result(self.results.pop(0))


def _row() -> dict[str, Any]:
    return {
        "id": UUID(DOCUMENT["snapshot_id"]),
        "match_id": MATCH_ID,
        "source": DOCUMENT["source"],
        "source_event_id": DOCUMENT["source_event_id"],
        "quoted_at": datetime.fromisoformat(DOCUMENT["quoted_at"]),
        "observed_at": datetime.fromisoformat(DOCUMENT["observed_at"]),
        "received_at": datetime.fromisoformat(DOCUMENT["received_at"]),
        "contract_version": DOCUMENT["contract_version"],
        "markets_json": {"markets": DOCUMENT["markets"]},
        "sha256": DOCUMENT["sha256"],
    }


def _adapter(*results: object) -> StoredMarketAdapter:
    repository = MarketSnapshotRepository(
        _Connection(*results),  # type: ignore[arg-type]
        SCHEMA,
        lambda source, source_event_id: str(MATCH_ID),
    )
    return StoredMarketAdapter(repository)


def test_persisted_adapter_distinguishes_available_stale_late_and_missing() -> None:
    available = _adapter(_row()).snapshot_as_of(
        match_id=str(MATCH_ID),
        cutoff_at=datetime(2026, 9, 20, 8, 30, tzinfo=UTC),
        max_age=timedelta(hours=1),
    )
    assert available.status is AvailabilityStatus.AVAILABLE
    assert available.snapshot is not None

    stale = _adapter(_row()).snapshot_as_of(
        match_id=str(MATCH_ID),
        cutoff_at=datetime(2026, 9, 20, 8, 30, tzinfo=UTC),
        max_age=timedelta(minutes=1),
    )
    assert stale.status is AvailabilityStatus.STALE

    late = _adapter(None, True).snapshot_as_of(
        match_id=str(MATCH_ID),
        cutoff_at=datetime(2026, 9, 20, 8, 0, 5, tzinfo=UTC),
        max_age=timedelta(hours=1),
    )
    assert late.status is AvailabilityStatus.LATE
    assert late.reason == "late_quote_received_after_cutoff"

    missing = _adapter(None, False).snapshot_as_of(
        match_id=str(MATCH_ID),
        cutoff_at=datetime(2026, 9, 20, 7, 0, tzinfo=UTC),
        max_age=timedelta(hours=1),
    )
    assert missing.status is AvailabilityStatus.MISSING


def test_persisted_adapter_rejects_naive_cutoff_and_negative_age() -> None:
    with pytest.raises(MarketContractError, match="timezone-aware"):
        _adapter().snapshot_as_of(
            match_id=str(MATCH_ID),
            cutoff_at=datetime(2026, 9, 20, 8, 30),
            max_age=timedelta(hours=1),
        )
    with pytest.raises(MarketContractError, match="non-negative"):
        _adapter().snapshot_as_of(
            match_id=str(MATCH_ID),
            cutoff_at=datetime(2026, 9, 20, 8, 30, tzinfo=UTC),
            max_age=timedelta(seconds=-1),
        )


@pytest.mark.parametrize(
    ("eligibility", "snapshot_id"),
    [
        (EvaluationEligibility.MISSING, None),
        (EvaluationEligibility.STALE, None),
        (EvaluationEligibility.LATE, None),
        (EvaluationEligibility.UNSUPPORTED, str(DOCUMENT["snapshot_id"])),
    ],
)
def test_market_evaluation_repository_preserves_exact_eligibility(
    eligibility: EvaluationEligibility,
    snapshot_id: str | None,
) -> None:
    evaluation_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    connection = _Connection(evaluation_id)
    repository = MarketEvaluationRepository(connection)  # type: ignore[arg-type]
    evaluation = MarketEvaluation(
        prediction_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        match_id=str(MATCH_ID),
        snapshot_id=snapshot_id,
        evaluator_version="market-evaluator-v1",
        eligibility=eligibility,
        reason=f"{eligibility.value}_reason",
        lines=(),
    )

    assert repository.add_evaluation(evaluation) == evaluation_id
    assert connection.executed[0][1]["eligibility"] == eligibility.value


def test_missing_market_evaluation_idempotency_uses_null_safe_identity() -> None:
    evaluation_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    connection = _Connection(
        None,
        (evaluation_id, {}, EvaluationEligibility.MISSING.value, "missing_reason"),
    )
    repository = MarketEvaluationRepository(connection)  # type: ignore[arg-type]
    evaluation = MarketEvaluation(
        prediction_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        match_id=str(MATCH_ID),
        snapshot_id=None,
        evaluator_version="market-evaluator-v1",
        eligibility=EvaluationEligibility.MISSING,
        reason="missing_reason",
        lines=(),
    )

    assert repository.add_evaluation(evaluation) == evaluation_id
    assert "IS NOT DISTINCT FROM" in connection.executed[1][0]
