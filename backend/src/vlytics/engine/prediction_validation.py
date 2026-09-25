"""JSON Schema and semantic validation for PredictionOutputV1."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from vlytics.engine.predictors.sets import SET_OUTCOME_ORDER

PREDICTION_MASS_TOLERANCE = 1e-6


class PredictionContractError(ValueError):
    """A structurally valid-looking prediction violates the server contract."""


@dataclass(frozen=True)
class JointScoreReferenceMetadata:
    """Persistence-owned metadata returned for a joint distribution reference."""

    distribution_id: str
    producer_variant_id: str
    input_snapshot_id: str
    home_win_probability: float
    set_score_probabilities: tuple[float, ...]


JointScoreResolver = Callable[[str], JointScoreReferenceMetadata | None]


@dataclass(frozen=True)
class PredictionValidationContext:
    """Expected ownership supplied by the prediction persistence boundary."""

    producer_variant_id: str | None = None
    input_snapshot_id: str | None = None
    joint_score_resolver: JointScoreResolver | None = None


def validate_prediction_output_v1(
    document: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    context: PredictionValidationContext | None = None,
) -> None:
    """Run Draft 2020-12 validation, then enforce cross-field probability semantics."""

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "$"
        raise PredictionContractError(f"prediction schema violation at {location}: {first.message}")
    _validate_semantics(document, context or PredictionValidationContext())


def _validate_semantics(
    document: Mapping[str, Any],
    context: PredictionValidationContext,
) -> None:
    producer_variant_id = _string(document, "producer_variant_id")
    capabilities = tuple(_strings(document, "capabilities"))
    capability_set = set(capabilities)
    provenance = _mapping(document, "target_provenance")
    if capability_set != set(provenance):
        raise PredictionContractError(
            "capabilities and target_provenance keys must be exactly equal"
        )
    if (
        context.producer_variant_id is not None
        and producer_variant_id != context.producer_variant_id
    ):
        raise PredictionContractError("producer variant does not match persistence context")

    has_winner = "home_win_probability" in document
    has_sets = "set_score_probabilities" in document
    has_joint = "joint_score_distribution_ref" in document
    if has_winner != ("winner" in capability_set):
        raise PredictionContractError("winner capability and home probability must agree")
    if has_sets != ("set_score" in capability_set):
        raise PredictionContractError("set_score capability and distribution must agree")
    point_capabilities = capability_set & {"point_handicap", "point_total"}
    if has_joint != bool(point_capabilities):
        raise PredictionContractError(
            "joint score reference must exist exactly when a point capability exists"
        )

    expected_snapshot: str | None = context.input_snapshot_id
    expected_statistical_lineage: tuple[Any, ...] | None = None
    for target, value in provenance.items():
        target_provenance = _mapping_value(value, f"target_provenance.{target}")
        if _string(target_provenance, "producer_variant_id") != producer_variant_id:
            raise PredictionContractError("target provenance producer variant is inconsistent")
        if _string(target_provenance, "distribution_version") != _string(
            document, "distribution_version"
        ):
            raise PredictionContractError("target provenance distribution version is inconsistent")
        snapshot_id = _string(target_provenance, "input_snapshot_id")
        if expected_snapshot is None:
            expected_snapshot = snapshot_id
        if snapshot_id != expected_snapshot:
            raise PredictionContractError("all targets must reference the same input snapshot")
        _validate_probability_source(target_provenance)
        statistical_lineage = _validate_statistical_lineage(target_provenance)
        if statistical_lineage is not None:
            if expected_statistical_lineage is None:
                expected_statistical_lineage = statistical_lineage
            elif statistical_lineage != expected_statistical_lineage:
                raise PredictionContractError(
                    "all statistical targets must preserve identical upstream lineage"
                )
    probabilities: tuple[float, ...] | None = None
    if has_sets:
        outcomes = document["set_score_probabilities"]
        if not isinstance(outcomes, Sequence) or isinstance(outcomes, str | bytes):
            raise PredictionContractError("set score probabilities must be an ordered array")
        labels: list[str] = []
        values: list[float] = []
        for index, raw in enumerate(outcomes):
            item = _mapping_value(raw, f"set_score_probabilities[{index}]")
            labels.append(_string(item, "outcome"))
            values.append(_probability(item, "probability"))
        if tuple(labels) != tuple(outcome.value for outcome in SET_OUTCOME_ORDER):
            raise PredictionContractError("set score outcomes are not in the fixed order")
        probabilities = tuple(values)
        if abs(sum(probabilities) - 1.0) > PREDICTION_MASS_TOLERANCE:
            raise PredictionContractError("set score probability mass must sum to one")
        winner = _probability(document, "home_win_probability")
        if abs(sum(probabilities[:3]) - winner) > PREDICTION_MASS_TOLERANCE:
            raise PredictionContractError("winner probability must equal the set-score marginal")
    elif has_winner:
        _probability(document, "home_win_probability")

    if has_joint:
        reference = _string(document, "joint_score_distribution_ref")
        resolver = context.joint_score_resolver
        if resolver is None:
            raise PredictionContractError("joint score references require a persistence resolver")
        metadata = resolver(reference)
        if metadata is None:
            raise PredictionContractError("joint score distribution reference was not found")
        if metadata.distribution_id != reference:
            raise PredictionContractError("resolved joint distribution ID is inconsistent")
        if metadata.producer_variant_id != producer_variant_id:
            raise PredictionContractError("joint distribution is owned by another variant")
        if expected_snapshot is None or metadata.input_snapshot_id != expected_snapshot:
            raise PredictionContractError("joint distribution uses another input snapshot")
        winner = _probability(document, "home_win_probability")
        if abs(metadata.home_win_probability - winner) > PREDICTION_MASS_TOLERANCE:
            raise PredictionContractError("joint distribution winner marginal is inconsistent")
        if probabilities is None or len(metadata.set_score_probabilities) != 6:
            raise PredictionContractError("joint distribution set marginal is unavailable")
        if any(
            abs(left - right) > PREDICTION_MASS_TOLERANCE
            for left, right in zip(
                metadata.set_score_probabilities,
                probabilities,
                strict=True,
            )
        ):
            raise PredictionContractError("joint distribution set marginal is inconsistent")


def _validate_statistical_lineage(
    provenance: Mapping[str, Any],
) -> tuple[Any, ...] | None:
    method = _string(provenance, "derivation_method")
    if method not in {
        "independent_set_distribution_marginalized_to_winner",
        "joint_score_distribution_marginalization",
    }:
        return None
    required = (
        "upstream_prediction_id",
        "upstream_model_version",
        "upstream_config_id",
        "fitted_parameter_artifact_id",
        "training_cohort",
        "training_as_of",
        "training_result_manifest_sha256",
    )
    missing = [field for field in required if field not in provenance]
    if missing:
        raise PredictionContractError(
            "statistical provenance is missing lineage: " + ", ".join(missing)
        )
    training_cohort = _mapping(provenance, "training_cohort")
    source_set_lineage: tuple[str, ...] = ()
    if method == "joint_score_distribution_marginalization":
        source_required = (
            "source_set_prediction_id",
            "source_set_producer_variant_id",
            "source_set_model_version",
            "source_set_config_id",
        )
        source_missing = [field for field in source_required if field not in provenance]
        if source_missing:
            raise PredictionContractError(
                "joint score provenance is missing set lineage: " + ", ".join(source_missing)
            )
        source_set_lineage = tuple(_string(provenance, field) for field in source_required)
    return (
        _string(provenance, "upstream_prediction_id"),
        _string(provenance, "upstream_model_version"),
        _string(provenance, "upstream_config_id"),
        _string(provenance, "fitted_parameter_artifact_id"),
        json.dumps(training_cohort, sort_keys=True, separators=(",", ":")),
        _string(provenance, "training_as_of"),
        _string(provenance, "training_result_manifest_sha256"),
        *source_set_lineage,
    )


def _validate_probability_source(provenance: Mapping[str, Any]) -> None:
    source = _string(provenance, "probability_source")
    method = _string(provenance, "derivation_method")
    expected_methods = {
        "ai_direct": {"ai_direct_structured_output"},
        "statistical_derived": {
            "independent_set_distribution_marginalized_to_winner",
            "joint_score_distribution_marginalization",
        },
        "hybrid_derived": {"ai_set_distribution_with_statistical_conditional_score"},
    }
    if source not in expected_methods or method not in expected_methods[source]:
        raise PredictionContractError("probability_source and derivation_method are inconsistent")
    if source == "ai_direct":
        if "prompt_version" not in provenance or "prompt_hash" not in provenance:
            raise PredictionContractError("AI provenance must preserve prompt lineage")
        _string(provenance, "prompt_version")
        prompt_hash = _string(provenance, "prompt_hash")
        if len(prompt_hash) != 64 or any(
            character not in "0123456789abcdef" for character in prompt_hash
        ):
            raise PredictionContractError("AI prompt_hash must be a lowercase SHA-256 digest")


def _probability(document: Mapping[str, Any], field: str) -> float:
    value = document[field]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PredictionContractError(f"{field} must be numeric")
    probability = float(value)
    if not isfinite(probability) or not 0 <= probability <= 1:
        raise PredictionContractError(f"{field} must be finite and between zero and one")
    return probability


def _mapping(document: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    return _mapping_value(document[field], field)


def _mapping_value(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PredictionContractError(f"{field} must be an object")
    return value


def _string(document: Mapping[str, Any], field: str) -> str:
    value = document[field]
    if not isinstance(value, str) or not value:
        raise PredictionContractError(f"{field} must be a non-empty string")
    return value


def _strings(document: Mapping[str, Any], field: str) -> list[str]:
    value = document[field]
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise PredictionContractError(f"{field} must be an array")
    if any(not isinstance(item, str) or not item for item in value):
        raise PredictionContractError(f"{field} must contain non-empty strings")
    return list(value)
