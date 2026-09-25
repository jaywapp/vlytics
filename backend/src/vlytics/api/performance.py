"""Server-owned performance aggregation for the operator API."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Literal, cast

from vlytics.api.models import (
    Availability,
    CalibrationBinSummary,
    PairedComparisonSummary,
    PairedMetricSummary,
    PerformanceMetrics,
    PerformanceRow,
)
from vlytics.api.repository import object_mapping
from vlytics.engine.evaluation.metrics import calibration_bins, paired_difference

_METRIC_NAMES = (
    "brier",
    "log_loss",
    "winner_accuracy",
    "set_rps",
    "set_score_accuracy",
)
_BASELINE_KINDS = ("home_rate", "statistical", "market")
BaselineKind = Literal["home_rate", "statistical", "market"]


@dataclass(frozen=True)
class PerformanceBuild:
    rows: list[PerformanceRow]
    evaluations: list[Mapping[str, Any]]


@dataclass
class _Group:
    key: tuple[str, ...]
    rows: list[Mapping[str, Any]]
    cohort: dict[str, str]
    provider: str
    model_version: str
    prompt_version: str
    prediction_type: str
    evaluator_version: str
    cohort_policy_version: str
    baseline_kind: BaselineKind | None


def build_performance(
    evaluations: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
) -> PerformanceBuild:
    """Select latest result revisions, aggregate exact cohorts, and compare common matches."""

    latest = _latest_evaluations(evaluations)
    groups = _group_evaluations(latest)
    _add_market_groups(groups)
    _add_missing_baseline_groups(groups)

    failures = _failure_counts(predictions)
    items_by_key: dict[tuple[str, ...], PerformanceRow] = {}
    for group in groups.values():
        items_by_key[group.key] = _aggregate_group(group, failures)

    for group in groups.values():
        if group.baseline_kind is not None:
            continue
        comparisons = _comparisons(group, groups)
        row = items_by_key[group.key]
        items_by_key[group.key] = row.model_copy(
            update={
                "comparisons": comparisons,
                "paired_sample_size": max(
                    (comparison.paired_n for comparison in comparisons), default=0
                ),
            }
        )

    ordered = sorted(
        items_by_key.values(),
        key=lambda row: (
            row.division,
            row.competition,
            json.dumps(row.cohort, sort_keys=True),
            row.provider,
            row.prediction_type,
            row.model_version,
        ),
    )
    return PerformanceBuild(ordered, latest)


def _latest_evaluations(
    rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    latest: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for row in rows:
        stored_cohort = object_mapping(object_mapping(row.get("metric_values")).get("cohort"))
        cohort_identity = json.dumps(
            {
                str(name): value
                for name, value in stored_cohort.items()
                if str(name) != "result_finality"
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        key = (
            str(row.get("prediction_id")),
            str(row.get("evaluator_version")),
            str(row.get("cohort_policy_version")),
            cohort_identity,
        )
        current = latest.get(key)
        if current is None or _revision_order(row) > _revision_order(current):
            latest[key] = row
    return list(latest.values())


def _revision_order(row: Mapping[str, Any]) -> tuple[int, str, str]:
    number = row.get("result_revision_number", 0)
    revision = int(number) if isinstance(number, int) and not isinstance(number, bool) else 0
    created = row.get("evaluation_created_at")
    timestamp = created.isoformat() if isinstance(created, datetime) else ""
    return revision, timestamp, str(row.get("id"))


def _group_evaluations(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, ...], _Group]:
    grouped: dict[tuple[str, ...], _Group] = {}
    for row in rows:
        metrics = object_mapping(row.get("metric_values"))
        cohort = {
            str(key): str(value) for key, value in object_mapping(metrics.get("cohort")).items()
        }
        for name in ("division", "competition", "provider", "model_version", "prompt_version"):
            cohort.setdefault(name, str(row.get(name)))
        cohort.setdefault("stage", str(row.get("stage", "other")))
        cohort.setdefault("feature_version", str(row.get("feature_version", "unknown")))
        cohort.setdefault(
            "availability_policy", str(row.get("availability_policy", "live_prospective"))
        )
        cohort.setdefault("timing_eligibility", str(row.get("timing_eligibility", "on_time")))
        cohort.setdefault("result_finality", str(row.get("result_finality", "final")))
        provider = str(row.get("provider"))
        prediction_type = str(row.get("prediction_type"))
        kind = _baseline_kind(provider, prediction_type, str(row.get("model_version")))
        key = _group_key(
            cohort,
            str(row.get("evaluator_version")),
            str(row.get("cohort_policy_version")),
            prediction_type,
            kind,
        )
        group = grouped.get(key)
        if group is None:
            group = _Group(
                key=key,
                rows=[],
                cohort=cohort,
                provider="baseline" if kind is not None else provider,
                model_version=str(row.get("model_version")),
                prompt_version=str(row.get("prompt_version")),
                prediction_type=kind or prediction_type,
                evaluator_version=str(row.get("evaluator_version")),
                cohort_policy_version=str(row.get("cohort_policy_version")),
                baseline_kind=kind,
            )
            grouped[key] = group
        group.rows.append(row)
    return grouped


def _group_key(
    cohort: Mapping[str, str],
    evaluator_version: str,
    cohort_policy_version: str,
    prediction_type: str,
    baseline_kind: BaselineKind | None,
) -> tuple[str, ...]:
    return (
        json.dumps(dict(cohort), sort_keys=True, separators=(",", ":")),
        evaluator_version,
        cohort_policy_version,
        prediction_type,
        baseline_kind or "ai",
    )


def _baseline_kind(provider: str, prediction_type: str, model_version: str) -> BaselineKind | None:
    normalized = " ".join((provider, prediction_type, model_version)).lower().replace("-", "_")
    if "home_rate" in normalized or "home win rate" in normalized:
        return "home_rate"
    if provider.lower() in {"statistical", "baseline"} or prediction_type.lower() == "statistical":
        return "statistical"
    if provider.lower() == "market" or prediction_type.lower() == "market":
        return "market"
    return None


def _context(group: _Group) -> tuple[str, ...]:
    cohort = group.cohort
    return (
        cohort["division"],
        cohort["competition"],
        cohort["stage"],
        cohort["feature_version"],
        cohort["availability_policy"],
        cohort["timing_eligibility"],
        cohort["result_finality"],
        group.evaluator_version,
        group.cohort_policy_version,
    )


def _add_missing_baseline_groups(groups: dict[tuple[str, ...], _Group]) -> None:
    ai_groups = [group for group in groups.values() if group.baseline_kind is None]
    for ai in ai_groups:
        for raw_kind in ("home_rate", "statistical"):
            kind: BaselineKind = raw_kind
            if any(
                group.baseline_kind == kind and _context(group) == _context(ai)
                for group in groups.values()
            ):
                continue
            cohort = {
                **ai.cohort,
                "provider": "baseline",
                "model_version": f"{kind}-unavailable",
                "prompt_version": "not-applicable",
            }
            key = _group_key(
                cohort,
                ai.evaluator_version,
                ai.cohort_policy_version,
                kind,
                kind,
            )
            groups[key] = _Group(
                key,
                [],
                cohort,
                "baseline",
                f"{kind}-unavailable",
                "not-applicable",
                kind,
                ai.evaluator_version,
                ai.cohort_policy_version,
                kind,
            )


def _add_market_groups(groups: dict[tuple[str, ...], _Group]) -> None:
    ai_groups = [group for group in groups.values() if group.baseline_kind is None]
    for ai in ai_groups:
        if any(
            group.baseline_kind == "market" and _context(group) == _context(ai)
            for group in groups.values()
        ):
            continue
        market_rows: list[Mapping[str, Any]] = []
        for row in ai.rows:
            probability = _market_home_probability(row)
            metrics = object_mapping(row.get("metric_values"))
            outcome = metrics.get("home_win_outcome")
            if probability is None or not isinstance(outcome, int) or isinstance(outcome, bool):
                continue
            market_rows.append(
                {
                    **row,
                    "provider": "market",
                    "model_version": "available-market-v1",
                    "prompt_version": "not-applicable",
                    "prediction_type": "market",
                    "metric_values": {
                        **metrics,
                        "cohort": {
                            **ai.cohort,
                            "provider": "market",
                            "model_version": "available-market-v1",
                            "prompt_version": "not-applicable",
                        },
                        "eligible": True,
                        "brier": (probability - outcome) ** 2,
                        "log_loss": _binary_log_loss(probability, outcome),
                        "winner_accuracy": float((probability >= 0.5) == bool(outcome)),
                        "set_rps": None,
                        "set_score_accuracy": None,
                        "home_win_probability": probability,
                    },
                }
            )
        cohort = {
            **ai.cohort,
            "provider": "market",
            "model_version": "available-market-v1",
            "prompt_version": "not-applicable",
        }
        key = _group_key(
            cohort,
            ai.evaluator_version,
            ai.cohort_policy_version,
            "market",
            "market",
        )
        groups[key] = _Group(
            key,
            market_rows,
            cohort,
            "baseline",
            "available-market-v1",
            "not-applicable",
            "market",
            ai.evaluator_version,
            ai.cohort_policy_version,
            "market",
        )


def _market_home_probability(row: Mapping[str, Any]) -> float | None:
    explicit = _number(row.get("market_home_probability"))
    if explicit is not None:
        return explicit
    payload = object_mapping(row.get("market_json"))
    lines = payload.get("markets")
    if not isinstance(lines, list):
        return None
    odds: dict[str, float] = {}
    for raw_line in lines:
        line = object_mapping(raw_line)
        if str(line.get("market_type")) != "moneyline" or str(line.get("period")) != "full_match":
            continue
        selection = str(line.get("selection"))
        try:
            price = float(str(line.get("decimal_odds")))
        except (TypeError, ValueError):
            continue
        if selection in {"home", "away"} and isfinite(price) and price > 1.0:
            odds[selection] = price
    if set(odds) != {"home", "away"}:
        return None
    home_implied = 1.0 / odds["home"]
    away_implied = 1.0 / odds["away"]
    return home_implied / (home_implied + away_implied)


def _binary_log_loss(probability: float, outcome: int) -> float:
    from math import log

    clipped = min(max(probability, 1e-12), 1.0 - 1e-12)
    return -(outcome * log(clipped) + (1 - outcome) * log(1.0 - clipped))


def _aggregate_group(
    group: _Group,
    failures: Mapping[tuple[str, ...], int],
) -> PerformanceRow:
    eligible = [
        row
        for row in group.rows
        if object_mapping(row.get("metric_values")).get("eligible", True) is True
    ]
    metric_values: dict[str, float | None] = {}
    for name in _METRIC_NAMES:
        values = [
            value
            for row in eligible
            if (value := _number(object_mapping(row.get("metric_values")).get(name))) is not None
        ]
        metric_values[name] = sum(values) / len(values) if values else None
    winner_n = sum(
        _number(object_mapping(row.get("metric_values")).get("brier")) is not None
        for row in eligible
    )
    set_n = sum(
        _number(object_mapping(row.get("metric_values")).get("set_rps")) is not None
        for row in eligible
    )
    observations: list[tuple[float, int]] = []
    for row in eligible:
        metrics = object_mapping(row.get("metric_values"))
        probability = _number(metrics.get("home_win_probability"))
        outcome = metrics.get("home_win_outcome")
        if probability is not None and isinstance(outcome, int) and not isinstance(outcome, bool):
            observations.append((probability, outcome))
    calibration = [
        CalibrationBinSummary(**item.to_dict()) for item in calibration_bins(observations)
    ]
    failure_count = failures.get(_failure_key_for_group(group), 0) if group.rows else 0
    availability = _market_availability(group.rows)
    return PerformanceRow(
        provider=group.provider,
        model_version=group.model_version,
        prompt_version=group.prompt_version,
        prediction_type=group.prediction_type,
        division=cast(Any, group.cohort["division"]),
        competition=group.cohort["competition"],
        cohort=group.cohort,
        sample_size=winner_n,
        failure_count=failure_count,
        excluded_count=len(group.rows) - len(eligible),
        corrected_evaluation_count=sum(
            row_cohort(row).get("result_finality") == "corrected" for row in eligible
        ),
        paired_sample_size=0,
        metrics=PerformanceMetrics(
            winner_n=winner_n,
            set_n=set_n,
            winner_accuracy=metric_values["winner_accuracy"],
            brier=metric_values["brier"],
            log_loss=metric_values["log_loss"],
            set_rps=metric_values["set_rps"],
            set_score_accuracy=metric_values["set_score_accuracy"],
        ),
        calibration=calibration,
        market_availability=availability,
        evaluator_version=group.evaluator_version,
        cohort_policy_version=group.cohort_policy_version,
        evaluation_revision_ids=sorted(str(row.get("id")) for row in group.rows),
        result_revision_ids=sorted(
            {
                str(row.get("result_revision_id"))
                for row in group.rows
                if row.get("result_revision_id")
            }
        ),
    )


def row_cohort(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in object_mapping(
            object_mapping(row.get("metric_values")).get("cohort")
        ).items()
    }


def _market_availability(rows: Sequence[Mapping[str, Any]]) -> Availability:
    statuses: set[str] = set()
    for row in rows:
        settlement = object_mapping(row.get("settlement"))
        status = settlement.get("market_status", row.get("market_availability", "missing"))
        statuses.add(str(status))
    if not statuses:
        return "missing"
    if statuses & {"eligible", "available"}:
        return "available"
    if "missing" in statuses or "not_requested" in statuses:
        return "missing"
    if "unsupported" in statuses or "not_supported" in statuses:
        return "not_supported"
    return "unverified"


def _failure_counts(
    predictions: Sequence[Mapping[str, Any]],
) -> Counter[tuple[str, ...]]:
    result: Counter[tuple[str, ...]] = Counter()
    for row in predictions:
        if str(row.get("provider_status", "succeeded")) == "succeeded":
            continue
        result[
            (
                str(row.get("division")),
                str(row.get("competition")),
                str(row.get("provider")),
                str(row.get("model_version")),
                str(row.get("prompt_version")),
                str(row.get("feature_version")),
                str(row.get("prediction_type")),
            )
        ] += 1
    return result


def _failure_key_for_group(group: _Group) -> tuple[str, ...]:
    return (
        group.cohort["division"],
        group.cohort["competition"],
        group.cohort["provider"],
        group.cohort["model_version"],
        group.cohort["prompt_version"],
        group.cohort["feature_version"],
        group.prediction_type,
    )


def _comparisons(
    ai: _Group,
    groups: Mapping[tuple[str, ...], _Group],
) -> list[PairedComparisonSummary]:
    candidates = [
        group
        for group in groups.values()
        if group.baseline_kind is not None and _context(group) == _context(ai)
    ]
    return [
        _comparison(ai, baseline)
        for baseline in sorted(candidates, key=lambda item: item.prediction_type)
    ]


def _comparison(ai: _Group, baseline: _Group) -> PairedComparisonSummary:
    ai_pairs = _eligible_pairs(ai.rows)
    baseline_pairs = _eligible_pairs(baseline.rows)
    common = sorted(set(ai_pairs) & set(baseline_pairs))
    brier_differences = [
        _required_metric(ai_pairs[key], "brier") - _required_metric(baseline_pairs[key], "brier")
        for key in common
    ]
    log_differences = [
        _required_metric(ai_pairs[key], "log_loss")
        - _required_metric(baseline_pairs[key], "log_loss")
        for key in common
    ]
    exclusions = {
        key: value
        for key, value in {
            "ai_unavailable": len(set(baseline_pairs) - set(ai_pairs)),
            "baseline_unavailable": len(set(ai_pairs) - set(baseline_pairs)),
        }.items()
        if value
    }
    brier = paired_difference(brier_differences, metric="brier")
    log_loss = paired_difference(log_differences, metric="log_loss")
    return PairedComparisonSummary(
        baseline_kind=cast(BaselineKind, baseline.baseline_kind),
        baseline_model_version=baseline.model_version,
        ai_individual_n=len(ai_pairs),
        baseline_individual_n=len(baseline_pairs),
        paired_n=len(common),
        excluded_reasons=exclusions,
        brier=PairedMetricSummary(**brier.to_dict()),
        log_loss=PairedMetricSummary(**log_loss.to_dict()),
    )


def _eligible_pairs(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, ...], Mapping[str, Any]]:
    result: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for row in rows:
        metrics = object_mapping(row.get("metric_values"))
        if metrics.get("eligible", True) is not True:
            continue
        if _number(metrics.get("brier")) is None or _number(metrics.get("log_loss")) is None:
            continue
        key_values = (
            row.get("match_id"),
            row.get("schedule_revision_id"),
            row.get("feature_snapshot_id"),
            row.get("input_cutoff_at"),
            row.get("result_revision_id"),
        )
        if any(value is None for value in key_values):
            continue
        result[tuple(str(value) for value in key_values)] = row
    return result


def _required_metric(row: Mapping[str, Any], name: str) -> float:
    value = _number(object_mapping(row.get("metric_values")).get(name))
    if value is None:
        raise ValueError(f"paired evaluation is missing {name}")
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    converted = float(value)
    return converted if isfinite(converted) else None
