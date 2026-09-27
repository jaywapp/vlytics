from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from vlytics.ops import health_evidence
from vlytics.ops.health_evidence import (
    DatabaseEvidence,
    ProviderLimit,
    collect_operations_evidence,
    load_provider_limits,
    merge_provider_budgets,
    write_evidence_atomic,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "contracts" / "config.schema.json"


def _live_config(tmp_path: Path) -> Path:
    content = (ROOT / "infra" / "operational.production.example.toml").read_text(encoding="utf-8")
    replacements = {
        'host_provider = "__REQUIRED__"': 'host_provider = "synthetic-host"',
        'host_region = "__REQUIRED__"': 'host_region = "synthetic-region"',
        "monthly_cost_limit = 0": "monthly_cost_limit = 100",
        'cost_currency = "__REQUIRED__"': 'cost_currency = "USD"',
        'strategy = "__REQUIRED__"': 'strategy = "synthetic-backup"',
        "interval_hours = 0": "interval_hours = 6",
        "retention_days = 0": "retention_days = 7",
        "rpo_minutes = 0": "rpo_minutes = 60",
        "rto_minutes = 0": "rto_minutes = 120",
        'channel = "__REQUIRED__"': 'channel = "synthetic-alert"',
        'budget_currency = "__REQUIRED__"': 'budget_currency = "USD"',
        'model_id = "__REQUIRED__"': 'model_id = "synthetic-model"',
        'version_policy = "unconfigured"': 'version_policy = "immutable_model_id"',
        'pinned_model_version = "__REQUIRED__"': 'pinned_model_version = "synthetic-v1"',
        "daily_budget_amount = 0": "daily_budget_amount = 2",
        "monthly_budget_amount = 0": "monthly_budget_amount = 20",
        "max_calls_per_match = 0": "max_calls_per_match = 1",
        "daily_call_limit = 0": "daily_call_limit = 10",
        "monthly_call_limit = 0": "monthly_call_limit = 100",
        "max_input_tokens_per_call = 0": "max_input_tokens_per_call = 1000",
        "max_output_tokens_per_call = 0": "max_output_tokens_per_call = 500",
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    path = tmp_path / "operational.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_provider_limits_validates_config_without_runtime_secrets(tmp_path: Path) -> None:
    limits = load_provider_limits(_live_config(tmp_path), SCHEMA)

    assert [item.provider for item in limits] == ["openai", "anthropic", "google"]
    assert all(item.currency == "USD" for item in limits)
    assert all(item.daily_amount == Decimal("2") for item in limits)
    assert all(item.monthly_calls == 100 for item in limits)


def test_merge_provider_budgets_fills_zero_usage_and_applies_limits() -> None:
    limits = (
        ProviderLimit("openai", "USD", Decimal("2"), Decimal("20"), 10, 100),
        ProviderLimit("google", "USD", Decimal("3"), Decimal("30"), 20, 200),
    )

    rows = merge_provider_budgets(
        limits,
        [
            {
                "provider": "openai",
                "currency": "USD",
                "daily_used_amount": "0.25",
                "daily_used_calls": 1,
                "monthly_used_amount": "1.25",
                "monthly_used_calls": 4,
            }
        ],
    )

    assert rows[0]["provider"] == "google"
    assert rows[0]["daily_used_amount"] == "0"
    assert rows[1]["daily_used_amount"] == "0.25"
    assert rows[1]["monthly_limit_calls"] == 100


def test_unconfigured_database_provider_fails_budget_evidence_closed(
    tmp_path: Path, monkeypatch: object
) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "schema_version": "worker-heartbeat-v1",
                "completed_at": "2026-09-27T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    ntp = tmp_path / "ntp.json"
    ntp.write_text(
        json.dumps(
            {
                "schema_version": "host-ntp-evidence-v1",
                "synchronized": True,
                "observed_at": "2026-09-27T00:00:00+00:00",
                "offset_ms": 0.5,
            }
        ),
        encoding="utf-8",
    )

    def fake_database(*, database_url: str, now: datetime) -> DatabaseEvidence:
        assert database_url == "synthetic-url"
        assert now == datetime(2026, 9, 27, tzinfo=UTC)
        return DatabaseEvidence(
            running_jobs=[],
            provider_usage=[
                {
                    "provider": "unexpected-provider",
                    "currency": "USD",
                    "daily_used_amount": "1",
                    "daily_used_calls": 1,
                    "monthly_used_amount": "1",
                    "monthly_used_calls": 1,
                }
            ],
        )

    monkeypatch.setattr(health_evidence, "collect_database_evidence", fake_database)  # type: ignore[attr-defined]
    evidence = collect_operations_evidence(
        database_url="synthetic-url",
        operational_config_path=_live_config(tmp_path),
        operational_schema_path=SCHEMA,
        heartbeat_path=heartbeat,
        ntp_evidence_path=ntp,
        now=datetime(2026, 9, 27, tzinfo=UTC),
    )

    assert evidence["worker_heartbeat_at"] == "2026-09-27T00:00:00+00:00"
    assert evidence["running_jobs"] == []
    assert evidence["provider_budgets"] is None
    assert "unexpected-provider" in evidence["expected_providers"]
    assert {
        "producer": "provider_budgets",
        "status": "error",
        "code": "unconfigured_providers",
        "providers": ["unexpected-provider"],
    } in evidence["diagnostics"]


def test_failed_producers_are_null_and_not_empty_success(tmp_path: Path) -> None:
    evidence = collect_operations_evidence(
        database_url=None,
        operational_config_path=ROOT / "config" / "example.toml",
        operational_schema_path=SCHEMA,
        heartbeat_path=tmp_path / "missing-heartbeat.json",
        ntp_evidence_path=tmp_path / "missing-ntp.json",
        now=datetime(2026, 9, 27, tzinfo=UTC),
    )

    assert evidence["worker_heartbeat_at"] is None
    assert evidence["ntp"] is None
    assert evidence["running_jobs"] is None
    assert evidence["provider_budgets"] is None
    assert {
        "producer": "database",
        "status": "error",
        "code": "credentials_unavailable",
    } in evidence["diagnostics"]


def test_cli_never_prints_or_writes_database_secret(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    secret = "postgresql+psycopg://monitor:unique-secret@private.invalid/vlytics"

    def failed_database(*, database_url: str, now: datetime) -> DatabaseEvidence:
        assert database_url == secret
        raise SQLAlchemyError(f"connection failed for {database_url}")

    monkeypatch.setattr(health_evidence, "collect_database_evidence", failed_database)  # type: ignore[attr-defined]
    monkeypatch.setenv("SYNTHETIC_HEALTH_DATABASE_URL", secret)  # type: ignore[attr-defined]
    output = tmp_path / "evidence.json"
    exit_code = health_evidence.main(
        [
            "--database-url-env",
            "SYNTHETIC_HEALTH_DATABASE_URL",
            "--operational-config",
            str(ROOT / "config" / "example.toml"),
            "--operational-schema",
            str(SCHEMA),
            "--heartbeat",
            str(tmp_path / "missing-heartbeat.json"),
            "--ntp-evidence",
            str(tmp_path / "missing-ntp.json"),
            "--output",
            str(output),
            "--now",
            "2026-09-27T00:00:00+00:00",
        ]
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 0
    assert secret not in captured.out
    assert secret not in captured.err
    assert secret not in output.read_text(encoding="utf-8")
    assert json.loads(output.read_text(encoding="utf-8"))["running_jobs"] is None


def test_atomic_writer_replaces_existing_document(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "evidence.json"
    output.parent.mkdir()
    output.write_text("stale", encoding="utf-8")

    write_evidence_atomic(output, {"schema_version": "operations-health-evidence-v1"})

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "schema_version": "operations-health-evidence-v1"
    }
    assert list(output.parent.glob("*.tmp")) == []


def test_unsafe_observed_provider_is_not_copied_into_error() -> None:
    secret_like_provider = "token=secret\nnext-line"
    limits = (ProviderLimit("openai", "USD", Decimal("2"), Decimal("20"), 10, 100),)

    with pytest.raises(ValueError) as raised:
        merge_provider_budgets(
            limits,
            [
                {
                    "provider": secret_like_provider,
                    "currency": "USD",
                    "daily_used_amount": "1",
                    "daily_used_calls": 1,
                    "monthly_used_amount": "1",
                    "monthly_used_calls": 1,
                }
            ],
        )

    assert str(raised.value) == "invalid_observed_provider:"
    assert secret_like_provider not in str(raised.value)
