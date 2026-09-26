"""Read-only operational health evaluation and JSON CLI reporting."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any


class HealthStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HealthThresholds:
    heartbeat_max_age: timedelta = timedelta(minutes=3)
    backup_max_age: timedelta = timedelta(hours=25)
    ntp_evidence_max_age: timedelta = timedelta(minutes=10)
    ntp_max_abs_offset_ms: float = 1000.0
    running_job_max_age: timedelta = timedelta(minutes=10)
    budget_warning_ratio: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        durations = (
            self.heartbeat_max_age,
            self.backup_max_age,
            self.ntp_evidence_max_age,
            self.running_job_max_age,
        )
        if any(value <= timedelta(0) for value in durations):
            raise ValueError("health age thresholds must be positive")
        if not isfinite(self.ntp_max_abs_offset_ms) or self.ntp_max_abs_offset_ms < 0:
            raise ValueError("NTP offset threshold must be non-negative")
        if not self.budget_warning_ratio.is_finite() or not Decimal(
            "0"
        ) < self.budget_warning_ratio < Decimal("1"):
            raise ValueError("budget warning ratio must be between zero and one")


@dataclass(frozen=True)
class HealthCheck:
    check_id: str
    status: HealthStatus
    summary: str
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "summary": self.summary,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class OperationsHealthReport:
    evaluated_at: datetime
    checks: tuple[HealthCheck, ...]

    @property
    def status(self) -> HealthStatus:
        statuses = {check.status for check in self.checks}
        if HealthStatus.CRITICAL in statuses:
            return HealthStatus.CRITICAL
        if HealthStatus.UNKNOWN in statuses:
            return HealthStatus.UNKNOWN
        if HealthStatus.WARNING in statuses:
            return HealthStatus.WARNING
        return HealthStatus.OK

    @property
    def exit_code(self) -> int:
        if self.status is HealthStatus.OK:
            return 0
        if self.status is HealthStatus.WARNING:
            return 2
        return 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "operations-health-report-v1",
            "evaluated_at": self.evaluated_at.astimezone(UTC).isoformat(),
            "status": self.status.value,
            "notification_required": self.status is not HealthStatus.OK,
            "notification_dispatched": False,
            "checks": [check.to_dict() for check in self.checks],
        }


def evaluate_heartbeat(
    heartbeat_at: datetime | None,
    *,
    now: datetime,
    max_age: timedelta,
) -> HealthCheck:
    _require_aware(now, "now")
    if heartbeat_at is None:
        return _unknown("worker_heartbeat", "worker heartbeat evidence was not supplied")
    _require_aware(heartbeat_at, "heartbeat_at")
    age = now - heartbeat_at
    evidence = {"heartbeat_at": _iso(heartbeat_at), "age_seconds": age.total_seconds()}
    if age < timedelta(0):
        return HealthCheck(
            "worker_heartbeat",
            HealthStatus.CRITICAL,
            "worker heartbeat is in the future",
            evidence,
        )
    if age > max_age:
        return HealthCheck(
            "worker_heartbeat",
            HealthStatus.CRITICAL,
            "worker heartbeat is stale",
            evidence,
        )
    return HealthCheck("worker_heartbeat", HealthStatus.OK, "worker heartbeat is fresh", evidence)


def evaluate_backup_manifest(
    manifest: Mapping[str, Any] | None,
    *,
    now: datetime,
    max_age: timedelta,
) -> HealthCheck:
    _require_aware(now, "now")
    if manifest is None:
        return _unknown("backup_freshness", "backup manifest evidence was not supplied")
    if manifest.get("schema_version") != "2.0":
        return _unknown("backup_freshness", "backup manifest schema is unsupported")
    created_at = _optional_datetime(manifest.get("created_at_utc"), "created_at_utc")
    if created_at is None:
        return _unknown("backup_freshness", "backup manifest creation time is missing")
    if manifest.get("preserves_owner_and_acl") is not True:
        return HealthCheck(
            "backup_freshness",
            HealthStatus.CRITICAL,
            "latest backup does not attest owner and ACL preservation",
            {"created_at_utc": _iso(created_at)},
        )
    sha256 = manifest.get("sha256")
    byte_length = manifest.get("byte_length")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        or isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or byte_length <= 0
    ):
        return _unknown("backup_freshness", "backup manifest integrity evidence is incomplete")
    age = now - created_at
    evidence = {
        "created_at_utc": _iso(created_at),
        "age_seconds": age.total_seconds(),
        "byte_length": byte_length,
        "sha256": sha256,
    }
    if age < timedelta(0):
        return HealthCheck(
            "backup_freshness",
            HealthStatus.CRITICAL,
            "backup manifest creation time is in the future",
            evidence,
        )
    if age > max_age:
        return HealthCheck(
            "backup_freshness",
            HealthStatus.CRITICAL,
            "latest backup manifest is stale",
            evidence,
        )
    return HealthCheck("backup_freshness", HealthStatus.OK, "latest backup is fresh", evidence)


def evaluate_ntp(
    ntp: Mapping[str, Any] | None,
    *,
    now: datetime,
    evidence_max_age: timedelta,
    max_abs_offset_ms: float,
) -> HealthCheck:
    _require_aware(now, "now")
    if ntp is None:
        return _unknown("ntp_sync", "NTP evidence was not supplied")
    synchronized = ntp.get("synchronized")
    observed_at = _optional_datetime(ntp.get("observed_at"), "ntp.observed_at")
    offset = ntp.get("offset_ms")
    if not isinstance(synchronized, bool) or observed_at is None:
        return _unknown("ntp_sync", "NTP synchronization state or observation time is missing")
    if isinstance(offset, bool) or not isinstance(offset, int | float):
        return _unknown("ntp_sync", "NTP offset evidence is missing")
    offset_ms = float(offset)
    if not isfinite(offset_ms):
        return _unknown("ntp_sync", "NTP offset evidence is invalid")
    age = now - observed_at
    evidence = {
        "synchronized": synchronized,
        "observed_at": _iso(observed_at),
        "age_seconds": age.total_seconds(),
        "offset_ms": offset_ms,
    }
    if age < timedelta(0):
        return HealthCheck(
            "ntp_sync", HealthStatus.CRITICAL, "NTP evidence is in the future", evidence
        )
    if age > evidence_max_age:
        return HealthCheck("ntp_sync", HealthStatus.CRITICAL, "NTP evidence is stale", evidence)
    if not synchronized:
        return HealthCheck(
            "ntp_sync", HealthStatus.CRITICAL, "host clock is not synchronized", evidence
        )
    if abs(offset_ms) > max_abs_offset_ms:
        return HealthCheck(
            "ntp_sync",
            HealthStatus.CRITICAL,
            "host clock offset exceeds the approved threshold",
            evidence,
        )
    return HealthCheck("ntp_sync", HealthStatus.OK, "host clock is synchronized", evidence)


def evaluate_running_jobs(
    jobs: Sequence[Mapping[str, Any]] | None,
    *,
    now: datetime,
    max_age: timedelta,
) -> HealthCheck:
    _require_aware(now, "now")
    if jobs is None:
        return _unknown("running_jobs", "running job evidence was not supplied")
    stale: list[dict[str, Any]] = []
    invalid: list[str] = []
    for job in jobs:
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            invalid.append("missing_job_id")
            continue
        started_at = _optional_datetime(job.get("lease_started_at"), "lease_started_at")
        lease_until = _optional_datetime(job.get("lease_until"), "lease_until")
        if started_at is None or lease_until is None:
            invalid.append(job_id)
            continue
        age = now - started_at
        if age < timedelta(0) or lease_until <= now or age > max_age:
            stale.append(
                {
                    "job_id": job_id,
                    "lease_started_at": _iso(started_at),
                    "lease_until": _iso(lease_until),
                    "age_seconds": age.total_seconds(),
                }
            )
    if invalid:
        return HealthCheck(
            "running_jobs",
            HealthStatus.UNKNOWN,
            "running job evidence contains incomplete rows",
            {"invalid_job_ids": invalid},
        )
    if stale:
        return HealthCheck(
            "running_jobs",
            HealthStatus.CRITICAL,
            "one or more running jobs are stale or have expired leases",
            {"stale_jobs": stale, "running_job_count": len(jobs)},
        )
    return HealthCheck(
        "running_jobs",
        HealthStatus.OK,
        "running jobs are within lease and age thresholds",
        {"running_job_count": len(jobs)},
    )


def evaluate_provider_budgets(
    budgets: Sequence[Mapping[str, Any]] | None,
    *,
    expected_providers: Sequence[str],
    warning_ratio: Decimal,
) -> HealthCheck:
    if budgets is None:
        return _unknown("provider_budgets", "provider budget evidence was not supplied")
    by_provider: dict[str, Mapping[str, Any]] = {}
    for row in budgets:
        provider = row.get("provider")
        if not isinstance(provider, str) or not provider.strip() or provider in by_provider:
            return _unknown("provider_budgets", "provider budget evidence has invalid identities")
        by_provider[provider] = row
    missing = sorted(set(expected_providers).difference(by_provider))
    if missing:
        return HealthCheck(
            "provider_budgets",
            HealthStatus.UNKNOWN,
            "provider budget evidence is incomplete",
            {"missing_providers": missing},
        )
    ratios: dict[str, dict[str, str]] = {}
    highest = Decimal("0")
    try:
        for provider in expected_providers:
            row = by_provider[provider]
            provider_ratios: dict[str, str] = {}
            for period in ("daily", "monthly"):
                used_amount = _decimal(row.get(f"{period}_used_amount"), f"{period}_used_amount")
                limit_amount = _positive_decimal(
                    row.get(f"{period}_limit_amount"), f"{period}_limit_amount"
                )
                used_calls = _decimal(row.get(f"{period}_used_calls"), f"{period}_used_calls")
                limit_calls = _positive_decimal(
                    row.get(f"{period}_limit_calls"), f"{period}_limit_calls"
                )
                if used_amount < 0 or used_calls < 0:
                    raise ValueError("provider budget usage must be non-negative")
                if used_calls != used_calls.to_integral_value():
                    raise ValueError("provider call usage must be an integer")
                amount_ratio = used_amount / limit_amount
                call_ratio = used_calls / limit_calls
                highest = max(highest, amount_ratio, call_ratio)
                provider_ratios[f"{period}_amount_ratio"] = str(amount_ratio)
                provider_ratios[f"{period}_call_ratio"] = str(call_ratio)
            ratios[provider] = provider_ratios
    except ValueError:
        return _unknown("provider_budgets", "provider budget evidence has invalid values")
    evidence = {"providers": ratios, "warning_ratio": str(warning_ratio)}
    if highest >= Decimal("1"):
        return HealthCheck(
            "provider_budgets",
            HealthStatus.CRITICAL,
            "a provider budget or call cap is exhausted",
            evidence,
        )
    if highest >= warning_ratio:
        return HealthCheck(
            "provider_budgets",
            HealthStatus.WARNING,
            "a provider budget or call cap is near exhaustion",
            evidence,
        )
    return HealthCheck(
        "provider_budgets",
        HealthStatus.OK,
        "provider budgets and call caps have headroom",
        evidence,
    )


def evaluate_operations_health(
    evidence: Mapping[str, Any],
    *,
    backup_manifest: Mapping[str, Any] | None,
    now: datetime,
    thresholds: HealthThresholds | None = None,
    expected_providers: Sequence[str] = ("openai", "anthropic", "google"),
) -> OperationsHealthReport:
    _require_aware(now, "now")
    thresholds = thresholds or HealthThresholds()
    if evidence.get("schema_version") != "operations-health-evidence-v1":
        raise ValueError("operations evidence schema is unsupported")
    if not expected_providers or len(expected_providers) != len(set(expected_providers)):
        raise ValueError("expected providers must be unique and non-empty")
    heartbeat = _optional_datetime(evidence.get("worker_heartbeat_at"), "worker_heartbeat_at")
    running_jobs = evidence.get("running_jobs")
    budgets = evidence.get("provider_budgets")
    return OperationsHealthReport(
        evaluated_at=now,
        checks=(
            evaluate_heartbeat(heartbeat, now=now, max_age=thresholds.heartbeat_max_age),
            evaluate_backup_manifest(backup_manifest, now=now, max_age=thresholds.backup_max_age),
            evaluate_ntp(
                evidence.get("ntp") if isinstance(evidence.get("ntp"), Mapping) else None,
                now=now,
                evidence_max_age=thresholds.ntp_evidence_max_age,
                max_abs_offset_ms=thresholds.ntp_max_abs_offset_ms,
            ),
            evaluate_running_jobs(
                running_jobs if isinstance(running_jobs, list) else None,
                now=now,
                max_age=thresholds.running_job_max_age,
            ),
            evaluate_provider_budgets(
                budgets if isinstance(budgets, list) else None,
                expected_providers=expected_providers,
                warning_ratio=thresholds.budget_warning_ratio,
            ),
        ),
    )


def load_latest_backup_manifest(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    candidates = [path] if path.is_file() else sorted(path.glob("*.manifest.json"))
    manifests: list[Mapping[str, Any]] = []
    for candidate in candidates:
        document = json.loads(candidate.read_text(encoding="utf-8"))
        if isinstance(document, Mapping):
            manifests.append(document)
    if not manifests:
        return None
    return max(
        manifests,
        key=lambda item: _optional_datetime(item.get("created_at_utc"), "created_at_utc")
        or datetime.min.replace(tzinfo=UTC),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate read-only vlytics operations evidence")
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--backup-manifest", type=Path)
    parser.add_argument("--now", help="UTC/offset ISO timestamp; defaults to current UTC")
    parser.add_argument("--heartbeat-max-age-seconds", type=int, default=180)
    parser.add_argument("--backup-max-age-hours", type=int, default=25)
    parser.add_argument("--ntp-evidence-max-age-seconds", type=int, default=600)
    parser.add_argument("--ntp-max-abs-offset-ms", type=float, default=1000.0)
    parser.add_argument("--running-job-max-age-seconds", type=int, default=600)
    parser.add_argument("--budget-warning-ratio", default="0.80")
    parser.add_argument(
        "--expected-provider",
        action="append",
        dest="expected_providers",
        default=None,
    )
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        if not isinstance(evidence, Mapping):
            raise ValueError("operations evidence must be a JSON object")
        now = _parse_datetime(args.now, "now") if args.now else datetime.now(UTC)
        thresholds = HealthThresholds(
            heartbeat_max_age=timedelta(seconds=args.heartbeat_max_age_seconds),
            backup_max_age=timedelta(hours=args.backup_max_age_hours),
            ntp_evidence_max_age=timedelta(seconds=args.ntp_evidence_max_age_seconds),
            ntp_max_abs_offset_ms=args.ntp_max_abs_offset_ms,
            running_job_max_age=timedelta(seconds=args.running_job_max_age_seconds),
            budget_warning_ratio=_decimal(args.budget_warning_ratio, "budget_warning_ratio"),
        )
        report = evaluate_operations_health(
            evidence,
            backup_manifest=load_latest_backup_manifest(args.backup_manifest),
            now=now,
            thresholds=thresholds,
            expected_providers=args.expected_providers or ("openai", "anthropic", "google"),
        )
        print(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))
        return report.exit_code
    except (OSError, ValueError, json.JSONDecodeError):
        print(
            json.dumps(
                {
                    "schema_version": "operations-health-report-v1",
                    "status": "unknown",
                    "notification_required": True,
                    "notification_dispatched": False,
                    "error": "operations evidence could not be evaluated",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 3


def _unknown(check_id: str, summary: str) -> HealthCheck:
    return HealthCheck(check_id, HealthStatus.UNKNOWN, summary, {"evidence_present": False})


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    _require_aware(parsed, name)
    return parsed


def _optional_datetime(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timestamp")
    return _parse_datetime(value, name)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _decimal(value: Any, name: str) -> Decimal:
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not converted.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return converted


def _positive_decimal(value: Any, name: str) -> Decimal:
    converted = _decimal(value, name)
    if converted <= 0:
        raise ValueError(f"{name} must be positive")
    return converted


if __name__ == "__main__":
    raise SystemExit(main())
