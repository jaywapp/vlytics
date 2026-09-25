"""Application and operational settings shared by API and worker processes."""

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

type JsonObject = dict[str, object]
type RuntimeComponent = Literal["api", "worker"]

_PLACEHOLDERS = frozenset(
    {
        "__REQUIRED__",
        "__REQUIRED_BY_OP_001__",
        "__REQUIRED_SECRET__",
        "CHANGE_ME",
        "UNCONFIGURED",
    }
)


class OperationalConfigError(RuntimeError):
    """Raised when the operational contract is missing or unsafe to activate."""


@dataclass(frozen=True)
class OperationalConfig:
    """Validated operational configuration with no resolved secret values."""

    values: JsonObject
    source_path: Path
    schema_path: Path

    @property
    def live_operations_enabled(self) -> bool:
        return _boolean(self.values, "live_operations_enabled")


def _discover_repository_root() -> Path:
    candidates = (Path.cwd(), Path.cwd().parent, *Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "config" / "example.toml").is_file() and (
            candidate / "contracts" / "config.schema.json"
        ).is_file():
            return candidate
    return Path.cwd()


def _default_operational_config_path() -> Path:
    return _discover_repository_root() / "config" / "example.toml"


def _default_operational_schema_path() -> Path:
    return _discover_repository_root() / "contracts" / "config.schema.json"


class Settings(BaseSettings):
    """Bootstrap settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="VLYTICS_", extra="ignore")

    environment: str = "development"
    database_url: str = Field(
        default="postgresql+psycopg://vlytics_read_api_login@localhost:5432/vlytics",
        repr=False,
    )
    migration_database_url: str | None = Field(default=None, repr=False)
    collector_database_password: SecretStr | None = Field(default=None, repr=False)
    engine_database_password: SecretStr | None = Field(default=None, repr=False)
    market_ingest_database_password: SecretStr | None = Field(default=None, repr=False)
    read_api_database_password: SecretStr | None = Field(default=None, repr=False)
    worker_poll_seconds: float = Field(default=5.0, gt=0)
    operational_config_path: Path = Field(default_factory=_default_operational_config_path)
    operational_schema_path: Path = Field(default_factory=_default_operational_schema_path)
    live_dry_run_evidence_path: Path | None = None


def _read_toml(path: Path) -> JsonObject:
    try:
        with path.open("rb") as stream:
            return cast(JsonObject, tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise OperationalConfigError(f"Cannot load operational config {path}: {error}") from error


def _read_schema(path: Path) -> JsonObject:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationalConfigError(f"Cannot load operational schema {path}: {error}") from error
    if not isinstance(parsed, dict):
        raise OperationalConfigError(f"Operational schema {path} must be a JSON object")
    return cast(JsonObject, parsed)


def _section(parent: Mapping[str, object], name: str) -> JsonObject:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise OperationalConfigError(f"Operational config section '{name}' must be an object")
    return cast(JsonObject, value)


def _string(parent: Mapping[str, object], name: str) -> str:
    value = parent.get(name)
    if not isinstance(value, str):
        raise OperationalConfigError(f"Operational config value '{name}' must be a string")
    return value


def _boolean(parent: Mapping[str, object], name: str) -> bool:
    value = parent.get(name)
    if not isinstance(value, bool):
        raise OperationalConfigError(f"Operational config value '{name}' must be a boolean")
    return value


def _integer(parent: Mapping[str, object], name: str) -> int:
    value = parent.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationalConfigError(f"Operational config value '{name}' must be an integer")
    return value


def _number(parent: Mapping[str, object], name: str) -> float:
    value = parent.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OperationalConfigError(f"Operational config value '{name}' must be a number")
    return float(value)


def _is_placeholder(value: str) -> bool:
    return value in _PLACEHOLDERS or not value.strip()


def _format_schema_error(error: object) -> str:
    absolute_path = getattr(error, "absolute_path", ())
    path = ".".join(str(part) for part in absolute_path) or "<root>"
    message = getattr(error, "message", str(error))
    return f"{path}: {message}"


def _require_configured(
    errors: list[str], section: Mapping[str, object], field: str, path: str
) -> None:
    value = _string(section, field)
    if _is_placeholder(value):
        errors.append(f"{path}.{field} must replace its placeholder before activation")


def _require_secret_reference(
    errors: list[str],
    section: Mapping[str, object],
    field: str,
    path: str,
    environ: Mapping[str, str],
) -> None:
    variable_name = _string(section, field)
    if not environ.get(variable_name):
        errors.append(f"{path}.{field} references missing environment variable {variable_name}")


def _validate_provider(
    errors: list[str],
    provider_name: str,
    provider: Mapping[str, object],
    environ: Mapping[str, str],
) -> None:
    if not _boolean(provider, "enabled"):
        errors.append(f"ai.{provider_name}.enabled must be true when live AI calls are enabled")
        return

    _require_configured(errors, provider, "model_id", f"ai.{provider_name}")
    _require_configured(errors, provider, "pinned_model_version", f"ai.{provider_name}")
    _require_secret_reference(errors, provider, "api_key_env", f"ai.{provider_name}", environ)

    daily_budget = _number(provider, "daily_budget_amount")
    monthly_budget = _number(provider, "monthly_budget_amount")
    if daily_budget <= 0 or monthly_budget <= 0:
        errors.append(f"ai.{provider_name} budgets must both be positive")
    elif daily_budget > monthly_budget:
        errors.append(f"ai.{provider_name} daily budget must not exceed monthly budget")

    daily_calls = _integer(provider, "daily_call_limit")
    monthly_calls = _integer(provider, "monthly_call_limit")
    if daily_calls <= 0 or monthly_calls <= 0:
        errors.append(f"ai.{provider_name} call limits must both be positive")
    elif daily_calls > monthly_calls:
        errors.append(f"ai.{provider_name} daily call limit must not exceed monthly call limit")


def _validate_timing(errors: list[str], timing: Mapping[str, object]) -> None:
    cutoff_seconds = _integer(timing, "target_cutoff_minutes") * 60
    tolerance = _integer(timing, "initial_start_tolerance_seconds")
    grace = _integer(timing, "completion_grace_seconds")
    request_timeout = _integer(timing, "request_timeout_seconds")
    attempts = _integer(timing, "max_attempts")
    initial_delay = _integer(timing, "retry_initial_delay_seconds")
    multiplier = _number(timing, "retry_backoff_multiplier")
    max_delay = _integer(timing, "retry_max_delay_seconds")

    if not 0 <= tolerance < grace < cutoff_seconds:
        errors.append(
            "timing must satisfy 0 <= initial start tolerance < completion grace < cutoff"
        )

    retry_delays = [
        min(initial_delay * multiplier**index, max_delay) for index in range(attempts - 1)
    ]
    worst_case_seconds = request_timeout * attempts + sum(retry_delays)
    if worst_case_seconds > grace:
        errors.append(
            "timing request timeouts and retry delays exceed the completion grace "
            f"({worst_case_seconds:g}s > {grace}s)"
        )

    if _boolean(timing, "allow_t10_retry"):
        errors.append("timing.allow_t10_retry must remain false")
    if _boolean(timing, "allow_completion_at_or_after_scheduled_start"):
        errors.append("completion at or after scheduled start must remain disabled")
    if _boolean(timing, "allow_completion_at_or_after_actual_start"):
        errors.append("completion at or after actual start must remain disabled")


def _validate_cross_fields(
    config: JsonObject,
    environ: Mapping[str, str],
    component: RuntimeComponent | None,
) -> None:
    errors: list[str] = []
    environment = _string(config, "environment")
    live_enabled = _boolean(config, "live_operations_enabled")
    deployment = _section(config, "deployment")
    database = _section(config, "database")
    backup = _section(config, "backup")
    alerting = _section(config, "alerting")
    ai = _section(config, "ai")
    source = _section(config, "source")
    timing = _section(config, "timing")

    if environment in {"staging", "production"} and not live_enabled:
        process = component or "runtime"
        errors.append(f"{process} cannot start in {environment} with live operations disabled")

    if _boolean(deployment, "live_enabled"):
        for field in ("host_provider", "host_region", "cost_currency"):
            _require_configured(errors, deployment, field, "deployment")
        _require_secret_reference(errors, deployment, "operator_secret_env", "deployment", environ)
        _require_secret_reference(errors, database, "connection_url_env", "database", environ)

    if _boolean(backup, "enabled"):
        _require_configured(errors, backup, "strategy", "backup")
        _require_secret_reference(errors, backup, "credential_env", "backup", environ)

    if _boolean(alerting, "enabled"):
        _require_configured(errors, alerting, "channel", "alerting")
        _require_secret_reference(errors, alerting, "destination_env", "alerting", environ)

    if _boolean(ai, "live_calls_enabled"):
        _require_configured(errors, ai, "budget_currency", "ai")
        for provider_name in ("openai", "anthropic", "google"):
            _validate_provider(
                errors,
                provider_name,
                _section(ai, provider_name),
                environ,
            )

    if _boolean(source, "bulk_collection_enabled"):
        _require_configured(errors, source, "permission_policy", "source")
        if _integer(source, "max_requests_per_minute") <= 0:
            errors.append("source.max_requests_per_minute must be positive for bulk collection")
        if _integer(source, "max_concurrency") <= 0:
            errors.append("source.max_concurrency must be positive for bulk collection")

    _validate_timing(errors, timing)

    if errors:
        raise OperationalConfigError("Invalid operational configuration: " + "; ".join(errors))


def validate_operational_config(
    config: JsonObject,
    schema: JsonObject,
    *,
    environ: Mapping[str, str] | None = None,
    component: RuntimeComponent | None = None,
) -> None:
    """Validate schema, activation gates, cross-field limits, and secret references."""

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise OperationalConfigError(f"Invalid operational JSON Schema: {error.message}") from error

    validator = Draft202012Validator(schema)
    schema_errors = sorted(
        validator.iter_errors(config),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if schema_errors:
        details = "; ".join(_format_schema_error(error) for error in schema_errors)
        raise OperationalConfigError(f"Operational config failed schema validation: {details}")

    _validate_cross_fields(config, environ if environ is not None else os.environ, component)


def load_operational_config(
    settings: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    component: RuntimeComponent | None = None,
) -> OperationalConfig:
    """Load TOML and its Draft 2020-12 schema, then apply runtime safety gates."""

    resolved_settings = settings or get_settings()
    config = _read_toml(resolved_settings.operational_config_path)
    schema = _read_schema(resolved_settings.operational_schema_path)
    validate_operational_config(config, schema, environ=environ, component=component)
    return OperationalConfig(
        values=config,
        source_path=resolved_settings.operational_config_path,
        schema_path=resolved_settings.operational_schema_path,
    )


def _require_aware(timestamp: datetime, name: str) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def prediction_deadline(
    config: OperationalConfig,
    scheduled_start_at: datetime,
    actual_start_at: datetime | None = None,
) -> datetime:
    """Compute the immutable deadline bounded by grace and all known match starts."""

    _require_aware(scheduled_start_at, "scheduled_start_at")
    if actual_start_at is not None:
        _require_aware(actual_start_at, "actual_start_at")

    timing = _section(config.values, "timing")
    cutoff_at = scheduled_start_at - timedelta(minutes=_integer(timing, "target_cutoff_minutes"))
    grace_deadline = cutoff_at + timedelta(seconds=_integer(timing, "completion_grace_seconds"))
    candidates = [grace_deadline, scheduled_start_at]
    if actual_start_at is not None:
        candidates.append(actual_start_at)
    return min(candidates)


def is_initial_start_eligible(
    config: OperationalConfig,
    started_at: datetime,
    scheduled_start_at: datetime,
) -> bool:
    """Return whether the first attempt started inside the strict T-60 window."""

    _require_aware(started_at, "started_at")
    _require_aware(scheduled_start_at, "scheduled_start_at")
    timing = _section(config.values, "timing")
    cutoff_at = scheduled_start_at - timedelta(minutes=_integer(timing, "target_cutoff_minutes"))
    latest_start = cutoff_at + timedelta(
        seconds=_integer(timing, "initial_start_tolerance_seconds")
    )
    return cutoff_at <= started_at <= latest_start


def is_prediction_completion_eligible(
    config: OperationalConfig,
    completed_at: datetime,
    scheduled_start_at: datetime,
    actual_start_at: datetime | None = None,
) -> bool:
    """Reject completion at the deadline or at/after either known match start."""

    _require_aware(completed_at, "completed_at")
    deadline_at = prediction_deadline(config, scheduled_start_at, actual_start_at)
    if completed_at >= deadline_at or completed_at >= scheduled_start_at:
        return False
    return actual_start_at is None or completed_at < actual_start_at


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings instance per process."""

    return Settings()
