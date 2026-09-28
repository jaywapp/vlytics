"""Production Market comparison path and immutable input regression tests."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from vlytics.engine.market import (
    EVALUATOR_VERSION,
    MARKET_COMPARISON_JOB_TYPE,
    AvailabilityStatus,
    EvaluationEligibility,
    MarketAvailability,
    MarketContractError,
    MarketSnapshot,
    PostgresPredictionMarketReader,
    market_comparison_job_key,
    market_handlers,
    validate_market_snapshot_v1,
)
from vlytics.worker import DatabaseRuntimeTicker

ROOT = Path(__file__).resolve().parents[3]
PREDICTION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
MATCH_ID = UUID("20000000-0000-4000-8000-000000000001")
SNAPSHOT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
VARIANT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
EVALUATION_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
CUTOFF = datetime(2026, 9, 20, 8, 30, tzinfo=UTC)
SET_PROBABILITIES = [
    {"outcome": "3:0", "probability": 0.2},
    {"outcome": "3:1", "probability": 0.2},
    {"outcome": "3:2", "probability": 0.2},
    {"outcome": "2:3", "probability": 0.15},
    {"outcome": "1:3", "probability": 0.15},
    {"outcome": "0:3", "probability": 0.1},
]


def _digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prediction_row() -> dict[str, object]:
    output = {
        "producer_variant_id": "variant-1",
        "home_win_probability": 0.6,
        "set_score_probabilities": SET_PROBABILITIES,
    }
    return {
        "id": PREDICTION_ID,
        "match_id": MATCH_ID,
        "snapshot_id": SNAPSHOT_ID,
        "variant_id": VARIANT_ID,
        "input_cutoff_at": CUTOFF,
        "output_json": output,
        "prediction_sha256": _digest(output),
        "hyperparameters": {"variant_key": "variant-1"},
        "distribution_id": None,
        "joint_match_id": None,
        "joint_snapshot_id": None,
        "joint_variant_id": None,
        "artifact_json": None,
        "joint_sha256": None,
    }


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> object:
        return self.value

    def scalar_one_or_none(self) -> object:
        return self.value

    def one(self) -> object:
        return self.value


class _Connection:
    def __init__(self) -> None:
        self.evaluation: dict[str, object] | None = None

    def execute(self, statement: object, parameters: dict[str, object] | None = None) -> _Result:
        sql = str(statement)
        values = parameters or {}
        if "FROM engine.predictions p" in sql:
            return _Result(_prediction_row())
        if "INSERT INTO market.market_evaluations" in sql:
            if self.evaluation is not None:
                return _Result(None)
            self.evaluation = {
                **values,
                "derived_probabilities": json.loads(str(values["derived_probabilities"])),
            }
            return _Result(EVALUATION_ID)
        if "SELECT id, derived_probabilities, eligibility, reason" in sql:
            assert self.evaluation is not None
            return _Result(
                (
                    EVALUATION_ID,
                    self.evaluation["derived_probabilities"],
                    self.evaluation["eligibility"],
                    self.evaluation["reason"],
                )
            )
        raise AssertionError(f"unexpected SQL: {sql}")


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    def begin(self) -> nullcontext[_Connection]:
        return nullcontext(self.connection)


class _Adapter:
    def __init__(self, availability: MarketAvailability) -> None:
        self.availability = availability
        self.calls: list[tuple[str, datetime]] = []

    def snapshot_as_of(
        self, *, match_id: str, cutoff_at: datetime, max_age: object
    ) -> MarketAvailability:
        del max_age
        self.calls.append((match_id, cutoff_at))
        return self.availability


def _market_snapshot() -> MarketSnapshot:
    schema = json.loads((ROOT / "contracts" / "market-v1.schema.json").read_text(encoding="utf-8"))
    document = json.loads(
        (ROOT / "fixtures" / "synthetic" / "market" / "full-match-v1.json").read_text(
            encoding="utf-8"
        )
    )
    return validate_market_snapshot_v1(document, schema)


@pytest.mark.parametrize(
    ("availability", "expected"),
    [
        (
            MarketAvailability(
                AvailabilityStatus.AVAILABLE,
                "synthetic_available",
                _market_snapshot(),
            ),
            EvaluationEligibility.ELIGIBLE,
        ),
        (
            MarketAvailability(AvailabilityStatus.STALE, "synthetic_stale"),
            EvaluationEligibility.STALE,
        ),
        (
            MarketAvailability(AvailabilityStatus.LATE, "synthetic_late"),
            EvaluationEligibility.LATE,
        ),
    ],
)
def test_production_handler_is_idempotent_for_synthetic_market_states(
    availability: MarketAvailability,
    expected: EvaluationEligibility,
) -> None:
    engine = _Engine()
    adapter = _Adapter(availability)
    handler = market_handlers(
        engine,  # type: ignore[arg-type]
        adapter_factory=lambda _connection: adapter,
        max_age=timedelta(hours=1),
    )[MARKET_COMPARISON_JOB_TYPE]
    job = {
        "job_type": MARKET_COMPARISON_JOB_TYPE,
        "payload": {
            "prediction_id": str(PREDICTION_ID),
            "evaluator_version": EVALUATOR_VERSION,
        },
    }

    handler.handle(job, now=CUTOFF)
    handler.handle(job, now=CUTOFF)

    assert engine.connection.evaluation is not None
    assert engine.connection.evaluation["eligibility"] == expected.value
    assert adapter.calls == [(str(MATCH_ID), CUTOFF), (str(MATCH_ID), CUTOFF)]
    assert market_comparison_job_key(PREDICTION_ID) == (
        f"market-comparison:{PREDICTION_ID}:{EVALUATOR_VERSION}"
    )


def test_production_handler_keeps_missing_adapter_as_explicit_missing() -> None:
    engine = _Engine()
    handler = market_handlers(engine)[MARKET_COMPARISON_JOB_TYPE]  # type: ignore[arg-type]
    job = {
        "job_type": MARKET_COMPARISON_JOB_TYPE,
        "payload": {
            "prediction_id": str(PREDICTION_ID),
            "evaluator_version": EVALUATOR_VERSION,
        },
    }

    handler.handle(job, now=CUTOFF)

    assert engine.connection.evaluation is not None
    assert engine.connection.evaluation["eligibility"] == EvaluationEligibility.MISSING.value
    assert engine.connection.evaluation["market_snapshot_id"] is None


def test_configured_market_adapter_requires_an_explicit_age_policy() -> None:
    engine = _Engine()
    with pytest.raises(ValueError, match="explicit max_age"):
        market_handlers(
            engine,  # type: ignore[arg-type]
            adapter_factory=lambda _connection: _Adapter(
                MarketAvailability(AvailabilityStatus.MISSING, "synthetic_missing")
            ),
        )


def test_persisted_prediction_marginals_require_exact_hash_and_ownership() -> None:
    distribution_id = "1" * 64
    artifact = {
        "distribution_id": distribution_id,
        "ownership": {
            "producer_variant_id": "variant-1",
            "input_snapshot_id": str(SNAPSHOT_ID),
        },
        "marginals": {
            "home_win_probability": 0.6,
            "set_score_probabilities": SET_PROBABILITIES,
            "point_totals": [{"total_points": 180, "probability": 1.0}],
            "point_differentials": [{"point_differential": 5, "probability": 1.0}],
        },
    }
    row = _prediction_row()
    output = dict(row["output_json"])  # type: ignore[arg-type]
    output["joint_score_distribution_ref"] = distribution_id
    row.update(
        {
            "output_json": output,
            "prediction_sha256": _digest(output),
            "distribution_id": distribution_id,
            "joint_match_id": MATCH_ID,
            "joint_snapshot_id": SNAPSHOT_ID,
            "joint_variant_id": VARIANT_ID,
            "artifact_json": artifact,
            "joint_sha256": _digest(artifact),
        }
    )

    prediction = PostgresPredictionMarketReader._from_row(row)  # type: ignore[arg-type]
    assert prediction.joint_score_distribution_id == distribution_id
    assert prediction.point_totals == {180: 1.0}
    assert prediction.point_differentials == {5: 1.0}

    row["joint_sha256"] = "0" * 64
    with pytest.raises(MarketContractError, match="artifact hash"):
        PostgresPredictionMarketReader._from_row(row)  # type: ignore[arg-type]


def test_worker_poll_discovers_market_before_result_evaluation() -> None:
    calls: list[str] = []

    class _Ticker:
        def __init__(self, name: str, count: int) -> None:
            self.name = name
            self.count = count

        def run_once(self, *, now: datetime) -> int:
            assert now == CUTOFF
            calls.append(self.name)
            return self.count

    ticker = DatabaseRuntimeTicker(
        _Ticker("schedule", 2),  # type: ignore[arg-type]
        _Ticker("evaluation", 4),  # type: ignore[arg-type]
        market=_Ticker("market", 3),  # type: ignore[arg-type]
        source=_Ticker("source", 1),  # type: ignore[arg-type]
    )

    assert ticker.run_once(now=CUTOFF) == 10
    assert calls == ["source", "schedule", "market", "evaluation"]
