import copy
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vlytics.api.app import create_app
from vlytics.config import (
    OperationalConfig,
    OperationalConfigError,
    Settings,
    is_initial_start_eligible,
    is_prediction_completion_eligible,
    load_operational_config,
    prediction_deadline,
    validate_operational_config,
)
from vlytics.ops.scheduler import load_live_dry_run_evidence, operational_config_sha256
from vlytics.worker import main as worker_main

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPOSITORY_ROOT / "config" / "example.toml"
CONFIG_SCHEMA = REPOSITORY_ROOT / "contracts" / "config.schema.json"


def _values() -> dict[str, object]:
    with EXAMPLE_CONFIG.open("rb") as stream:
        return tomllib.load(stream)


def _schema() -> dict[str, object]:
    return json.loads(CONFIG_SCHEMA.read_text(encoding="utf-8"))


def _settings(config_path: Path = EXAMPLE_CONFIG) -> Settings:
    return Settings(
        operational_config_path=config_path,
        operational_schema_path=CONFIG_SCHEMA,
    )


def _activate(values: dict[str, object]) -> dict[str, str]:
    values["environment"] = "production"
    values["live_operations_enabled"] = True

    deployment = values["deployment"]
    assert isinstance(deployment, dict)
    deployment.update(
        {
            "live_enabled": True,
            "host_class": "managed_vm_and_database",
            "host_provider": "synthetic-host",
            "host_region": "test-region",
            "monthly_cost_limit": 100,
            "cost_currency": "KRW",
            "network_access": "private_network",
            "operator_auth_method": "mutual_tls",
        }
    )

    backup = values["backup"]
    assert isinstance(backup, dict)
    backup.update(
        {
            "enabled": True,
            "strategy": "synthetic-backup",
            "interval_hours": 6,
            "retention_days": 7,
            "rpo_minutes": 60,
            "rto_minutes": 120,
        }
    )

    alerting = values["alerting"]
    assert isinstance(alerting, dict)
    alerting.update({"enabled": True, "channel": "synthetic-alert"})

    ai = values["ai"]
    assert isinstance(ai, dict)
    ai.update({"live_calls_enabled": True, "budget_currency": "KRW"})
    for provider_name in ("openai", "anthropic", "google"):
        provider = ai[provider_name]
        assert isinstance(provider, dict)
        provider.update(
            {
                "enabled": True,
                "model_id": f"synthetic-{provider_name}-model",
                "version_policy": "immutable_model_id",
                "pinned_model_version": f"synthetic-{provider_name}-v1",
                "daily_budget_amount": 1,
                "monthly_budget_amount": 10,
                "max_calls_per_match": 1,
                "daily_call_limit": 10,
                "monthly_call_limit": 100,
                "max_input_tokens_per_call": 1000,
                "max_output_tokens_per_call": 500,
            }
        )

    return {
        "VLYTICS_DATABASE_URL": "synthetic-database-secret",
        "VLYTICS_OPERATOR_AUTH_SECRET": "synthetic-operator-secret",
        "VLYTICS_BACKUP_CREDENTIAL": "synthetic-backup-secret",
        "VLYTICS_ALERT_DESTINATION": "synthetic-alert-secret",
        "VLYTICS_OPENAI_API_KEY": "synthetic-openai-secret",
        "VLYTICS_ANTHROPIC_API_KEY": "synthetic-anthropic-secret",
        "VLYTICS_GOOGLE_API_KEY": "synthetic-google-secret",
    }


def test_disabled_example_loads_with_draft_2020_12_schema() -> None:
    config = load_operational_config(_settings(), environ={}, component="worker")

    assert config.live_operations_enabled is False
    assert config.source_path == EXAMPLE_CONFIG
    assert config.schema_path == CONFIG_SCHEMA


def test_complete_synthetic_live_config_passes_without_bulk_source() -> None:
    values = _values()
    environ = _activate(values)

    validate_operational_config(values, _schema(), environ=environ, component="api")


def test_live_config_rejects_placeholders_and_unset_model_ids() -> None:
    values = _values()
    values["environment"] = "production"
    values["live_operations_enabled"] = True

    with pytest.raises(OperationalConfigError, match="schema validation"):
        validate_operational_config(values, _schema(), environ={}, component="api")


def test_live_config_rejects_daily_budget_above_monthly_budget() -> None:
    values = _values()
    environ = _activate(values)
    ai = values["ai"]
    assert isinstance(ai, dict)
    openai = ai["openai"]
    assert isinstance(openai, dict)
    openai["daily_budget_amount"] = 11

    with pytest.raises(OperationalConfigError, match="daily budget must not exceed"):
        validate_operational_config(values, _schema(), environ=environ, component="worker")


def test_live_config_rejects_missing_secret_reference_value() -> None:
    values = _values()
    environ = _activate(values)
    del environ["VLYTICS_GOOGLE_API_KEY"]

    with pytest.raises(OperationalConfigError, match="VLYTICS_GOOGLE_API_KEY"):
        validate_operational_config(values, _schema(), environ=environ, component="api")


def test_bulk_collection_rejects_unresolved_op_001_policy() -> None:
    values = _values()
    source = values["source"]
    assert isinstance(source, dict)
    source["bulk_collection_enabled"] = True

    with pytest.raises(OperationalConfigError, match="source"):
        validate_operational_config(values, _schema(), environ={}, component="worker")


def test_cross_field_validation_rejects_retry_budget_above_grace() -> None:
    values = _values()
    timing = values["timing"]
    assert isinstance(timing, dict)
    timing["request_timeout_seconds"] = 120

    schema = copy.deepcopy(_schema())
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    timing_definition = definitions["timing"]
    assert isinstance(timing_definition, dict)
    timing_properties = timing_definition["properties"]
    assert isinstance(timing_properties, dict)
    request_timeout = timing_properties["request_timeout_seconds"]
    assert isinstance(request_timeout, dict)
    request_timeout["const"] = 120

    with pytest.raises(OperationalConfigError, match="exceed the completion grace"):
        validate_operational_config(values, schema, environ={}, component="worker")


def test_deadline_and_eligibility_never_cross_match_start() -> None:
    config = load_operational_config(_settings(), environ={})
    scheduled_start = datetime(2026, 9, 20, 12, tzinfo=UTC)
    cutoff = scheduled_start - timedelta(minutes=60)
    normal_deadline = cutoff + timedelta(seconds=300)

    assert prediction_deadline(config, scheduled_start) == normal_deadline
    assert is_initial_start_eligible(config, cutoff, scheduled_start)
    assert is_initial_start_eligible(config, cutoff + timedelta(seconds=30), scheduled_start)
    assert not is_initial_start_eligible(config, cutoff + timedelta(seconds=31), scheduled_start)
    assert is_prediction_completion_eligible(
        config, normal_deadline - timedelta(microseconds=1), scheduled_start
    )
    assert not is_prediction_completion_eligible(config, normal_deadline, scheduled_start)

    early_actual_start = cutoff + timedelta(seconds=120)
    assert prediction_deadline(config, scheduled_start, early_actual_start) == early_actual_start
    assert not is_prediction_completion_eligible(
        config, early_actual_start, scheduled_start, early_actual_start
    )


def test_api_and_worker_fail_fast_for_disabled_production_config(tmp_path: Path) -> None:
    config_path = tmp_path / "production.toml"
    config_path.write_text(
        EXAMPLE_CONFIG.read_text(encoding="utf-8").replace(
            'environment = "example"', 'environment = "production"'
        ),
        encoding="utf-8",
    )
    settings = _settings(config_path)

    with pytest.raises(OperationalConfigError, match="api cannot start"):
        create_app(settings, environ={})
    assert worker_main(["--once"], settings=settings, environ={}) == 2


def test_api_and_worker_fail_fast_for_live_placeholders(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid-live.toml"
    invalid_live = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    invalid_live = invalid_live.replace('environment = "example"', 'environment = "production"')
    invalid_live = invalid_live.replace(
        "live_operations_enabled = false", "live_operations_enabled = true"
    )
    config_path.write_text(invalid_live, encoding="utf-8")
    settings = _settings(config_path)

    with pytest.raises(OperationalConfigError, match="schema validation"):
        create_app(settings, environ={})
    assert worker_main(["--once"], settings=settings, environ={}) == 2


def _live_operational_config() -> OperationalConfig:
    values = _values()
    _activate(values)
    return OperationalConfig(values, EXAMPLE_CONFIG, CONFIG_SCHEMA)


def _write_evidence(
    path: Path,
    config: OperationalConfig,
    *,
    completed_at: datetime,
    config_sha256: str | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "config_sha256": config_sha256 or operational_config_sha256(config),
                "completed_at": completed_at.isoformat(),
                "source_sync_verified": True,
                "freeze_verified": True,
                "providers_verified": ["openai", "anthropic", "google"],
            }
        ),
        encoding="utf-8",
    )


def test_live_dry_run_evidence_loader_accepts_fresh_exact_document(tmp_path: Path) -> None:
    config = _live_operational_config()
    now = datetime(2026, 9, 21, 3, tzinfo=UTC)
    path = tmp_path / "evidence.json"
    _write_evidence(path, config, completed_at=now - timedelta(hours=1))

    evidence = load_live_dry_run_evidence(config, path, now=now)

    assert evidence.config_sha256 == operational_config_sha256(config)
    assert evidence.providers_verified == ("openai", "anthropic", "google")


@pytest.mark.parametrize(
    ("completed_delta", "hash_override", "message"),
    [
        (timedelta(hours=-25), None, "expired"),
        (timedelta(minutes=6), None, "future"),
        (timedelta(hours=-1), "f" * 64, "does not match"),
    ],
)
def test_live_dry_run_evidence_loader_rejects_stale_future_and_hash_mismatch(
    tmp_path: Path,
    completed_delta: timedelta,
    hash_override: str | None,
    message: str,
) -> None:
    config = _live_operational_config()
    now = datetime(2026, 9, 21, 3, tzinfo=UTC)
    path = tmp_path / "evidence.json"
    _write_evidence(
        path,
        config,
        completed_at=now + completed_delta,
        config_sha256=hash_override,
    )

    with pytest.raises(OperationalConfigError, match=message):
        load_live_dry_run_evidence(config, path, now=now)


def test_live_dry_run_evidence_loader_rejects_malformed_and_oversized_files(
    tmp_path: Path,
) -> None:
    config = _live_operational_config()
    now = datetime(2026, 9, 21, 3, tzinfo=UTC)
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"schema_version":"1.0"}', encoding="utf-8")
    with pytest.raises(OperationalConfigError, match="document is invalid"):
        load_live_dry_run_evidence(config, malformed, now=now)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * 16385)
    with pytest.raises(OperationalConfigError, match="file is invalid"):
        load_live_dry_run_evidence(config, oversized, now=now)
