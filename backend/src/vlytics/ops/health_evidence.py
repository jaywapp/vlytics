"""Collect secret-free, read-only operational health evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import cast

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from vlytics.config import OperationalConfigError, validate_operational_config

_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PLACEHOLDERS = frozenset(
    {"__REQUIRED__", "__REQUIRED_BY_OP_001__", "__REQUIRED_SECRET__", "CHANGE_ME", "UNCONFIGURED"}
)


@dataclass(frozen=True)
class ProviderLimit:
    provider: str
    currency: str
    daily_amount: Decimal
    monthly_amount: Decimal
    daily_calls: int
    monthly_calls: int


@dataclass(frozen=True)
class DatabaseEvidence:
    running_jobs: list[dict[str, object]]
    provider_usage: list[dict[str, object]]


def collect_operations_evidence(
    *,
    database_url: str | None,
    operational_config_path: Path,
    operational_schema_path: Path,
    heartbeat_path: Path,
    ntp_evidence_path: Path,
    now: datetime,
) -> dict[str, object]:
    """Collect each independent producer and preserve missing-versus-empty semantics."""

    _require_aware(now, "now")
    diagnostics: list[dict[str, object]] = []

    worker_heartbeat_at = _collect_heartbeat(heartbeat_path, diagnostics)
    ntp = _collect_ntp(ntp_evidence_path, diagnostics)

    limits: tuple[ProviderLimit, ...] | None
    try:
        limits = load_provider_limits(operational_config_path, operational_schema_path)
    except (
        OSError,
        ValueError,
        OperationalConfigError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ):
        limits = None
        diagnostics.append(_diagnostic("provider_configuration", "error", "configuration_invalid"))
    else:
        diagnostics.append(_diagnostic("provider_configuration", "ok", "collected"))

    database: DatabaseEvidence | None = None
    if database_url is None:
        diagnostics.append(_diagnostic("database", "error", "credentials_unavailable"))
    else:
        try:
            database = collect_database_evidence(database_url=database_url, now=now)
        except (SQLAlchemyError, OSError, ValueError):
            diagnostics.append(_diagnostic("database", "error", "query_failed"))
        else:
            diagnostics.append(_diagnostic("database", "ok", "collected"))

    running_jobs: list[dict[str, object]] | None = None
    budgets: list[dict[str, object]] | None = None
    expected_providers: list[str] = []
    if database is not None:
        running_jobs = database.running_jobs
    if limits is not None:
        expected_providers = sorted(item.provider for item in limits)
    if database is not None and limits is not None:
        try:
            budgets = merge_provider_budgets(limits, database.provider_usage)
        except ValueError as error:
            code, providers = _budget_merge_diagnostic(error)
            diagnostic = _diagnostic("provider_budgets", "error", code)
            if providers:
                diagnostic["providers"] = providers
                expected_providers = sorted(set(expected_providers).union(providers))
            diagnostics.append(diagnostic)
        else:
            diagnostics.append(_diagnostic("provider_budgets", "ok", "collected"))
    else:
        diagnostics.append(_diagnostic("provider_budgets", "error", "dependency_unavailable"))

    return {
        "schema_version": "operations-health-evidence-v1",
        "collected_at": _iso(now),
        "worker_heartbeat_at": worker_heartbeat_at,
        "ntp": ntp,
        "running_jobs": running_jobs,
        "provider_budgets": budgets,
        "expected_providers": expected_providers,
        "diagnostics": diagnostics,
    }


def load_provider_limits(config_path: Path, schema_path: Path) -> tuple[ProviderLimit, ...]:
    """Validate the approved config without resolving unrelated runtime credentials."""

    with config_path.open("rb") as stream:
        config = cast(dict[str, object], tomllib.load(stream))
    schema_value = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema_value, dict):
        raise ValueError("operational schema must be an object")

    # The monitor consumes no operator, provider, backup, or notification secret. Supplying
    # sentinels for referenced API credentials retains all non-secret API validation while
    # preventing the monitor process from gaining unrelated production credentials.
    validation_environment = _referenced_api_credentials(config)
    validate_operational_config(
        config,
        cast(dict[str, object], schema_value),
        environ=validation_environment,
        component="api",
    )

    ai = _mapping(config.get("ai"), "ai")
    if ai.get("live_calls_enabled") is not True:
        return ()
    currency = _currency(ai.get("budget_currency"))
    limits: list[ProviderLimit] = []
    for provider in ("openai", "anthropic", "google"):
        values = _mapping(ai.get(provider), f"ai.{provider}")
        if values.get("enabled") is not True:
            continue
        limits.append(
            ProviderLimit(
                provider=provider,
                currency=currency,
                daily_amount=_positive_decimal(values.get("daily_budget_amount")),
                monthly_amount=_positive_decimal(values.get("monthly_budget_amount")),
                daily_calls=_positive_int(values.get("daily_call_limit")),
                monthly_calls=_positive_int(values.get("monthly_call_limit")),
            )
        )
    return tuple(limits)


def collect_database_evidence(*, database_url: str, now: datetime) -> DatabaseEvidence:
    """Read leases and current UTC budget periods in one read-only transaction."""

    _require_aware(now, "now")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        return collect_database_evidence_with_engine(engine=engine, now=now)
    finally:
        engine.dispose()


def collect_database_evidence_with_engine(*, engine: Engine, now: datetime) -> DatabaseEvidence:
    """Engine-injected database collector used by integration tests."""

    _require_aware(now, "now")
    budget_day = now.astimezone(UTC).date()
    budget_month = date(budget_day.year, budget_day.month, 1)
    with engine.connect() as connection, connection.begin():
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        read_only = connection.execute(text("SHOW transaction_read_only")).scalar_one()
        if read_only != "on":
            raise ValueError("database transaction is not read-only")
        job_rows = connection.execute(
            text(
                """
                SELECT id::text AS job_id, lease_started_at, lease_until
                FROM ops.jobs
                WHERE state = 'running'
                ORDER BY lease_started_at, id
                """
            )
        ).mappings()
        running_jobs: list[dict[str, object]] = [
            {
                "job_id": cast(str, row["job_id"]),
                "lease_started_at": _iso(cast(datetime, row["lease_started_at"])),
                "lease_until": _iso(cast(datetime, row["lease_until"])),
            }
            for row in job_rows
        ]
        usage_rows = connection.execute(
            text(
                """
                SELECT
                    provider,
                    currency,
                    COALESCE(SUM(
                        CASE WHEN budget_day = :budget_day
                            THEN COALESCE(settled_amount, reserved_amount)
                            ELSE 0 END
                    ), 0) AS daily_used_amount,
                    COUNT(*) FILTER (WHERE budget_day = :budget_day) AS daily_used_calls,
                    COALESCE(SUM(
                        CASE WHEN budget_month = :budget_month
                            THEN COALESCE(settled_amount, reserved_amount)
                            ELSE 0 END
                    ), 0) AS monthly_used_amount,
                    COUNT(*) FILTER (WHERE budget_month = :budget_month) AS monthly_used_calls
                FROM ops.provider_budget_reservations
                WHERE budget_day = :budget_day OR budget_month = :budget_month
                GROUP BY provider, currency
                ORDER BY provider, currency
                """
            ),
            {"budget_day": budget_day, "budget_month": budget_month},
        ).mappings()
        provider_usage = [
            {
                "provider": cast(str, row["provider"]),
                "currency": cast(str, row["currency"]),
                "daily_used_amount": str(cast(Decimal, row["daily_used_amount"])),
                "daily_used_calls": int(cast(int, row["daily_used_calls"])),
                "monthly_used_amount": str(cast(Decimal, row["monthly_used_amount"])),
                "monthly_used_calls": int(cast(int, row["monthly_used_calls"])),
            }
            for row in usage_rows
        ]
    return DatabaseEvidence(running_jobs=running_jobs, provider_usage=provider_usage)


def merge_provider_budgets(
    limits: Sequence[ProviderLimit], usage: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """Combine approved limits with usage, failing closed on unknown identities."""

    by_provider = {item.provider: item for item in limits}
    if len(by_provider) != len(limits):
        raise ValueError("duplicate_configured_provider:")
    usage_by_provider: dict[str, Mapping[str, object]] = {}
    for row in usage:
        provider = row.get("provider")
        if not isinstance(provider, str) or _SAFE_PROVIDER.fullmatch(provider) is None:
            raise ValueError("invalid_observed_provider:")
        if provider not in by_provider:
            raise ValueError(f"unconfigured_providers:{provider}")
        if provider in usage_by_provider:
            raise ValueError(f"multiple_provider_currencies:{provider}")
        currency = row.get("currency")
        if currency != by_provider[provider].currency:
            raise ValueError(f"provider_currency_mismatch:{provider}")
        usage_by_provider[provider] = row

    result: list[dict[str, object]] = []
    for provider in sorted(by_provider):
        limit = by_provider[provider]
        row = usage_by_provider.get(provider, {})
        result.append(
            {
                "provider": provider,
                "currency": limit.currency,
                "daily_used_amount": _usage_decimal(row.get("daily_used_amount", "0")),
                "daily_limit_amount": str(limit.daily_amount),
                "daily_used_calls": _usage_calls(row.get("daily_used_calls", 0)),
                "daily_limit_calls": limit.daily_calls,
                "monthly_used_amount": _usage_decimal(row.get("monthly_used_amount", "0")),
                "monthly_limit_amount": str(limit.monthly_amount),
                "monthly_used_calls": _usage_calls(row.get("monthly_used_calls", 0)),
                "monthly_limit_calls": limit.monthly_calls,
            }
        )
    return result


def write_evidence_atomic(path: Path, evidence: Mapping[str, object]) -> None:
    """Write evidence atomically in the destination directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(evidence, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect read-only vlytics operations evidence")
    parser.add_argument("--database-url-env", required=True)
    parser.add_argument("--operational-config", required=True, type=Path)
    parser.add_argument("--operational-schema", required=True, type=Path)
    parser.add_argument("--heartbeat", required=True, type=Path)
    parser.add_argument("--ntp-evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--now", help="UTC/offset ISO timestamp; defaults to current UTC")
    args = parser.parse_args(argv)

    try:
        if not _ENVIRONMENT_VARIABLE.fullmatch(args.database_url_env):
            raise ValueError("database URL environment variable name is invalid")
        now = _parse_datetime(args.now) if args.now else datetime.now(UTC)
        evidence = collect_operations_evidence(
            database_url=os.environ.get(args.database_url_env),
            operational_config_path=args.operational_config,
            operational_schema_path=args.operational_schema,
            heartbeat_path=args.heartbeat,
            ntp_evidence_path=args.ntp_evidence,
            now=now,
        )
        write_evidence_atomic(args.output, evidence)
    except (OSError, ValueError, SQLAlchemyError):
        print('{"error":"operations evidence could not be written","status":"unknown"}')
        return 3
    print('{"schema_version":"operations-health-evidence-v1","status":"written"}')
    return 0


def _collect_heartbeat(path: Path, diagnostics: list[dict[str, object]]) -> str | None:
    try:
        value = _read_json_object(path)
        if value.get("schema_version") != "worker-heartbeat-v1":
            raise ValueError("unsupported heartbeat schema")
        completed_at = value.get("completed_at")
        if not isinstance(completed_at, str):
            raise ValueError("heartbeat timestamp is missing")
        parsed = _parse_datetime(completed_at)
    except FileNotFoundError:
        diagnostics.append(_diagnostic("worker_heartbeat", "error", "file_missing"))
        return None
    except (OSError, ValueError, json.JSONDecodeError):
        diagnostics.append(_diagnostic("worker_heartbeat", "error", "evidence_invalid"))
        return None
    diagnostics.append(_diagnostic("worker_heartbeat", "ok", "collected"))
    return _iso(parsed)


def _collect_ntp(path: Path, diagnostics: list[dict[str, object]]) -> dict[str, object] | None:
    try:
        value = _read_json_object(path)
        if value.get("schema_version") != "host-ntp-evidence-v1":
            raise ValueError("unsupported NTP schema")
        synchronized = value.get("synchronized")
        observed_at = value.get("observed_at")
        offset_ms = value.get("offset_ms")
        if not isinstance(synchronized, bool) or not isinstance(observed_at, str):
            raise ValueError("NTP evidence is incomplete")
        if isinstance(offset_ms, bool) or not isinstance(offset_ms, int | float):
            raise ValueError("NTP offset is invalid")
        parsed = _parse_datetime(observed_at)
        numeric_offset = float(offset_ms)
        if not isfinite(numeric_offset):
            raise ValueError("NTP offset is invalid")
    except FileNotFoundError:
        diagnostics.append(_diagnostic("ntp", "error", "file_missing"))
        return None
    except (OSError, ValueError, json.JSONDecodeError):
        diagnostics.append(_diagnostic("ntp", "error", "evidence_invalid"))
        return None
    diagnostics.append(_diagnostic("ntp", "ok", "collected"))
    return {
        "synchronized": synchronized,
        "observed_at": _iso(parsed),
        "offset_ms": numeric_offset,
    }


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("evidence must be an object")
    return cast(dict[str, object], value)


def _referenced_api_credentials(config: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for section_name, field_name in (
        ("deployment", "operator_secret_env"),
        ("database", "connection_url_env"),
    ):
        section = config.get(section_name)
        if not isinstance(section, Mapping):
            continue
        reference = section.get(field_name)
        if (
            isinstance(reference, str)
            and reference not in _PLACEHOLDERS
            and _ENVIRONMENT_VARIABLE.fullmatch(reference)
        ):
            result[reference] = "configured-for-validation"
    return result


def _budget_merge_diagnostic(error: ValueError) -> tuple[str, list[str]]:
    code, separator, provider_text = str(error).partition(":")
    allowed = {
        "duplicate_configured_provider",
        "invalid_observed_provider",
        "unconfigured_providers",
        "multiple_provider_currencies",
        "provider_currency_mismatch",
        "usage_invalid",
    }
    safe_code = code if code in allowed else "budget_merge_failed"
    providers = sorted(value for value in provider_text.split(",") if separator and value.strip())
    return safe_code, providers


def _diagnostic(producer: str, status: str, code: str) -> dict[str, object]:
    return {"producer": producer, "status": status, "code": code}


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _currency(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("budget currency must be an ISO code")
    return value


def _positive_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("budget must be positive")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("budget must be positive") from error
    if not converted.is_finite() or converted <= 0:
        raise ValueError("budget must be positive")
    return converted


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("call limit must be positive")
    return value


def _usage_decimal(value: object) -> str:
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("usage_invalid:") from error
    if not converted.is_finite() or converted < 0:
        raise ValueError("usage_invalid:")
    return str(converted)


def _usage_calls(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("usage_invalid:")
    return value


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timestamp must be ISO 8601") from error
    _require_aware(parsed, "timestamp")
    return parsed


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
