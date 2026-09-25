"""Validated live-provider plans derived only from OperationalConfig."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import cast

from vlytics.config import (
    OperationalConfig,
    OperationalConfigError,
    validate_operational_config,
)

from .models import ProviderName, ProviderVariant, VersionPolicy
from .registry import VariantRegistry


@dataclass(frozen=True)
class RuntimeProviderPlan:
    variant: ProviderVariant
    api_key_env: str
    daily_budget: Decimal
    monthly_budget: Decimal
    max_calls_per_match: int
    daily_call_limit: int
    monthly_call_limit: int


@dataclass(frozen=True)
class LiveProviderPlan:
    budget_currency: str
    providers: tuple[RuntimeProviderPlan, ...]


def build_live_provider_plan(
    config: OperationalConfig,
    registry: VariantRegistry,
    *,
    environ: Mapping[str, str],
) -> LiveProviderPlan:
    """Fail closed unless live identity, pricing, limits, budgets, and secrets agree."""

    try:
        schema = json.loads(config.schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationalConfigError("operational config schema cannot be loaded") from error
    if not isinstance(schema, dict):
        raise OperationalConfigError("operational config schema must be an object")
    validate_operational_config(
        config.values,
        cast(dict[str, object], schema),
        environ=environ,
        component="worker",
    )
    values = config.values
    if values.get("live_operations_enabled") is not True:
        raise OperationalConfigError("live operations are disabled")
    ai = _mapping(values.get("ai"), "ai")
    if ai.get("live_calls_enabled") is not True:
        raise OperationalConfigError("live AI calls are disabled")
    currency = _string(ai.get("budget_currency"), "ai.budget_currency")
    if not registry.live_ready:
        raise OperationalConfigError("provider variant registry is not live-ready")

    by_provider = {variant.provider: variant for variant in registry.variants}
    plans: list[RuntimeProviderPlan] = []
    for provider_name in ProviderName:
        raw = _mapping(ai.get(provider_name.value), f"ai.{provider_name.value}")
        if raw.get("enabled") is not True:
            raise OperationalConfigError(f"ai.{provider_name.value} is disabled")
        variant = by_provider[provider_name]
        model_id = _string(raw.get("model_id"), f"ai.{provider_name.value}.model_id")
        pinned = _string(
            raw.get("pinned_model_version"),
            f"ai.{provider_name.value}.pinned_model_version",
        )
        try:
            policy = VersionPolicy(
                _string(raw.get("version_policy"), f"ai.{provider_name.value}.version_policy")
            )
        except ValueError as error:
            raise OperationalConfigError(
                f"ai.{provider_name.value}.version_policy is invalid"
            ) from error
        if (
            model_id != variant.requested_model_id
            or pinned != variant.pinned_model_version
            or policy is not variant.version_policy
        ):
            raise OperationalConfigError(
                f"ai.{provider_name.value} model identity differs from the variant registry"
            )
        if variant.input_cost_per_million <= 0 or variant.output_cost_per_million <= 0:
            raise OperationalConfigError(f"ai.{provider_name.value} pricing must be positive")
        max_input = _positive_int(
            raw.get("max_input_tokens_per_call"),
            f"ai.{provider_name.value}.max_input_tokens_per_call",
        )
        max_output = _positive_int(
            raw.get("max_output_tokens_per_call"),
            f"ai.{provider_name.value}.max_output_tokens_per_call",
        )
        daily_budget = _positive_decimal(
            raw.get("daily_budget_amount"),
            f"ai.{provider_name.value}.daily_budget_amount",
        )
        monthly_budget = _positive_decimal(
            raw.get("monthly_budget_amount"),
            f"ai.{provider_name.value}.monthly_budget_amount",
        )
        if daily_budget > monthly_budget:
            raise OperationalConfigError(
                f"ai.{provider_name.value} daily budget exceeds monthly budget"
            )
        api_key_env = _string(
            raw.get("api_key_env"),
            f"ai.{provider_name.value}.api_key_env",
        )
        if not environ.get(api_key_env):
            raise OperationalConfigError(
                f"ai.{provider_name.value}.api_key_env has no resolved secret"
            )
        plans.append(
            RuntimeProviderPlan(
                variant=replace(
                    variant,
                    max_input_tokens=max_input,
                    max_output_tokens=max_output,
                ),
                api_key_env=api_key_env,
                daily_budget=daily_budget,
                monthly_budget=monthly_budget,
                max_calls_per_match=_positive_int(
                    raw.get("max_calls_per_match"),
                    f"ai.{provider_name.value}.max_calls_per_match",
                ),
                daily_call_limit=_positive_int(
                    raw.get("daily_call_limit"),
                    f"ai.{provider_name.value}.daily_call_limit",
                ),
                monthly_call_limit=_positive_int(
                    raw.get("monthly_call_limit"),
                    f"ai.{provider_name.value}.monthly_call_limit",
                ),
            )
        )

    if sum((plan.daily_budget for plan in plans), Decimal("0")) > registry.daily_budget_amount:
        raise OperationalConfigError("configured daily AI budgets exceed the registry cap")
    if sum((plan.monthly_budget for plan in plans), Decimal("0")) > registry.monthly_budget_amount:
        raise OperationalConfigError("configured monthly AI budgets exceed the registry cap")
    return LiveProviderPlan(budget_currency=currency, providers=tuple(plans))


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OperationalConfigError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperationalConfigError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperationalConfigError(f"{name} must be a positive integer")
    return value


def _positive_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise OperationalConfigError(f"{name} must be positive")
    return Decimal(str(value))
