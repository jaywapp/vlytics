"""Exercise the checked-in home package through its real operational contracts."""

from decimal import Decimal
from pathlib import Path

import pytest

from vlytics.config import OperationalConfigError, Settings, load_operational_config
from vlytics.engine.providers import build_live_provider_plan, load_variant_registry
from vlytics.engine.providers.models import ProviderName

ROOT = Path(__file__).resolve().parents[2]
HOME_CONFIG = ROOT / "infra/operational.home.example.toml"
HOME_REGISTRY = ROOT / "config/variants.home.toml"


def _settings(config_path: Path = HOME_CONFIG) -> Settings:
    return Settings(
        operational_config_path=config_path,
        operational_schema_path=ROOT / "contracts/config.schema.json",
        provider_registry_path=HOME_REGISTRY,
    )


def _test_secrets() -> dict[str, str]:
    return {
        "VLYTICS_DATABASE_URL": "synthetic-database",
        "VLYTICS_OPERATOR_AUTH_SECRET": "synthetic-operator",
        "VLYTICS_OPENAI_API_KEY": "synthetic-openai",
    }


def _resolved_test_config(tmp_path):
    path = tmp_path / "home.toml"
    path.write_text(
        HOME_CONFIG.read_text().replace("__REQUIRED_AFTER_MODEL_SMOKE__", "synthetic-snapshot-v1")
    )
    config = load_operational_config(_settings(path), component="worker", environ=_test_secrets())
    config.values["activation"]["provider_registry_sha256"] = _resolved_test_registry(
        tmp_path
    ).content_sha256
    return config


def _resolved_test_registry(tmp_path):
    path = tmp_path / "variants.toml"
    path.write_text(
        HOME_REGISTRY.read_text()
        .replace("__REQUIRED_AFTER_MODEL_SMOKE__", "synthetic-snapshot-v1")
        .replace("op003_resolved = false", "op003_resolved = true", 1)
    )
    return load_variant_registry(path)


def test_home_example_cannot_start_before_secrets_and_model_smoke():
    with pytest.raises(OperationalConfigError):
        load_operational_config(_settings(), component="worker", environ={})
    with pytest.raises(OperationalConfigError, match="pinned_model_version"):
        load_operational_config(_settings(), component="worker", environ=_test_secrets())
    registry = load_variant_registry(HOME_REGISTRY)
    assert not registry.live_ready


def test_home_conversion_is_required_even_after_synthetic_model_smoke(tmp_path):
    config = _resolved_test_config(tmp_path)
    registry = _resolved_test_registry(tmp_path)
    with pytest.raises(OperationalConfigError, match="pricing_to_budget_rate"):
        build_live_provider_plan(config, registry, environ=_test_secrets())


def test_home_package_uses_only_openai_and_reserves_in_krw(tmp_path):
    config = _resolved_test_config(tmp_path)
    # Synthetic test conversion; this is deliberately absent from operational templates.
    config.values["ai"]["pricing_to_budget_rate"] = 1500
    registry = _resolved_test_registry(tmp_path)
    plan = build_live_provider_plan(config, registry, environ=_test_secrets())
    assert config.deployment_profile == "personal_home"
    assert config.enabled_providers == ("openai",)
    assert config.values["source"]["bulk_collection_enabled"] is False
    assert config.values["backup"]["enabled"] is False
    assert config.values["alerting"]["enabled"] is False
    assert plan.budget_currency == "KRW"
    assert len(plan.providers) == 1
    provider = plan.providers[0]
    assert provider.variant.provider is ProviderName.OPENAI
    assert provider.daily_budget == Decimal("1000")
    assert provider.monthly_budget == Decimal("10000")
    assert provider.variant.maximum_call_cost == Decimal("30")


def test_price_change_requires_new_reviewed_registry_and_dry_run(tmp_path):
    config = _resolved_test_config(tmp_path)
    config.values["ai"]["pricing_to_budget_rate"] = 1500
    registry = _resolved_test_registry(tmp_path)
    plan = build_live_provider_plan(config, registry, environ=_test_secrets())
    assert len(plan.providers) == 1
    path = tmp_path / "variants.toml"
    path.write_text(
        path.read_text().replace(
            'input_cost_per_million = "2.50"', 'input_cost_per_million = "3.00"'
        )
    )
    changed_registry = load_variant_registry(path)
    with pytest.raises(OperationalConfigError, match="provider_registry_sha256"):
        build_live_provider_plan(config, changed_registry, environ=_test_secrets())


def test_home_environment_example_contains_no_disabled_secrets():
    values = dict(
        line.split("=", 1)
        for line in (ROOT / "infra/.env.home.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert values["VLYTICS_OPENAI_API_KEY"] == "__REQUIRED__"
    for name in (
        "VLYTICS_ANTHROPIC_API_KEY",
        "VLYTICS_GOOGLE_API_KEY",
        "VLYTICS_BACKUP_CREDENTIAL",
        "VLYTICS_ALERT_DESTINATION",
    ):
        assert values[name] == ""
