"""Load the small reviewed provider variant registry from TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .base import PROMPT_TEMPLATE_HASH
from .models import ProviderName, ProviderVariant, VersionPolicy


@dataclass(frozen=True)
class VariantRegistry:
    schema_version: str
    variants: tuple[ProviderVariant, ...]
    budget_resolved: bool
    daily_budget_amount: Decimal
    monthly_budget_amount: Decimal

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise ValueError("unsupported provider variant registry schema")
        providers = [variant.provider for variant in self.variants]
        if len(providers) != len(set(providers)):
            raise ValueError("provider variant registry contains a duplicate provider")
        if set(providers) != set(ProviderName):
            raise ValueError("provider variant registry must contain exactly three providers")
        if any(variant.prompt_hash != PROMPT_TEMPLATE_HASH for variant in self.variants):
            raise ValueError("provider variant prompt hash does not match the rendered prompt")
        if self.daily_budget_amount < 0 or self.monthly_budget_amount < 0:
            raise ValueError("provider budgets must not be negative")
        if self.budget_resolved and (
            self.daily_budget_amount == 0 or self.monthly_budget_amount == 0
        ):
            raise ValueError("resolved provider budgets must be positive")

    @property
    def live_ready(self) -> bool:
        return self.budget_resolved and all(variant.operational for variant in self.variants)


def load_variant_registry(path: Path) -> VariantRegistry:
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    raw_variants = document.get("variants")
    if not isinstance(raw_variants, list):
        raise ValueError("provider variant registry requires [[variants]] entries")
    variants = tuple(_parse_variant(value) for value in raw_variants)
    budget = _mapping(document.get("budget"), "budget")
    return VariantRegistry(
        schema_version=_string(document.get("schema_version"), "schema_version"),
        variants=variants,
        budget_resolved=_boolean(budget.get("resolved"), "budget.resolved"),
        daily_budget_amount=_decimal(
            budget.get("daily_amount"),
            "budget.daily_amount",
        ),
        monthly_budget_amount=_decimal(
            budget.get("monthly_amount"),
            "budget.monthly_amount",
        ),
    )


def _parse_variant(value: Any) -> ProviderVariant:
    document = _mapping(value, "variant")
    try:
        provider = ProviderName(_string(document.get("provider"), "variant.provider"))
    except ValueError as error:
        raise ValueError("variant.provider is not supported") from error
    return ProviderVariant(
        variant_id=_string(document.get("id"), "variant.id"),
        provider=provider,
        requested_model_id=_string(
            document.get("requested_model_id"),
            "variant.requested_model_id",
        ),
        pinned_model_version=_string(
            document.get("pinned_model_version"),
            "variant.pinned_model_version",
        ),
        version_policy=VersionPolicy(
            _string(document.get("version_policy"), "variant.version_policy")
        ),
        prompt_version=_string(
            document.get("prompt_version"),
            "variant.prompt_version",
        ),
        prompt_hash=_string(document.get("prompt_hash"), "variant.prompt_hash"),
        distribution_version=_string(
            document.get("distribution_version"),
            "variant.distribution_version",
        ),
        max_input_tokens=_integer(
            document.get("max_input_tokens"),
            "variant.max_input_tokens",
        ),
        max_output_tokens=_integer(
            document.get("max_output_tokens"),
            "variant.max_output_tokens",
        ),
        input_cost_per_million=_decimal(
            document.get("input_cost_per_million"),
            "variant.input_cost_per_million",
        ),
        output_cost_per_million=_decimal(
            document.get("output_cost_per_million"),
            "variant.output_cost_per_million",
        ),
        enabled=_boolean(document.get("enabled"), "variant.enabled"),
        op003_resolved=_boolean(
            document.get("op003_resolved"),
            "variant.op003_resolved",
        ),
    )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal string") from error
