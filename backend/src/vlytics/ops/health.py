"""Read-only operational health evaluation and JSON CLI reporting."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any, cast


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
    database_evidence_max_age: timedelta = timedelta(minutes=10)
    budget_warning_ratio: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        durations = (
            self.heartbeat_max_age,
            self.backup_max_age,
            self.ntp_evidence_max_age,
            self.running_job_max_age,
            self.database_evidence_max_age,
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


def evaluate_backup_artifact(
    manifest: Mapping[str, Any] | None, *, artifact_path: Path | None
) -> HealthCheck:
    if manifest is None or artifact_path is None:
        return _unknown("backup_integrity", "backup artifact evidence was not supplied")
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
        return _unknown("backup_integrity", "backup manifest integrity evidence is incomplete")
    evidence = {"byte_length": byte_length, "sha256": sha256}
    try:
        if not artifact_path.is_file() or artifact_path.stat().st_size != byte_length:
            return HealthCheck(
                "backup_integrity",
                HealthStatus.CRITICAL,
                "backup artifact is missing or has the wrong size",
                evidence,
            )
        with artifact_path.open("rb") as stream:
            actual_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return HealthCheck(
            "backup_integrity",
            HealthStatus.CRITICAL,
            "backup artifact could not be verified",
            evidence,
        )
    if actual_sha256 != sha256:
        return HealthCheck(
            "backup_integrity",
            HealthStatus.CRITICAL,
            "backup artifact hash does not match the manifest",
            evidence,
        )
    return HealthCheck(
        "backup_integrity", HealthStatus.OK, "backup artifact matches the manifest", evidence
    )


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
        if not isinstance(job, Mapping):
            invalid.append("invalid_row")
            continue
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
        if not isinstance(row, Mapping):
            return _unknown("provider_budgets", "provider budget evidence has invalid rows")
        provider = row.get("provider")
        if not isinstance(provider, str) or not provider.strip() or provider in by_provider:
            return _unknown("provider_budgets", "provider budget evidence has invalid identities")
        by_provider[provider] = row
    missing = sorted(set(expected_providers).difference(by_provider))
    unexpected = sorted(set(by_provider).difference(expected_providers))
    if missing or unexpected:
        return HealthCheck(
            "provider_budgets",
            HealthStatus.UNKNOWN,
            "provider budget evidence is incomplete",
            {"missing_providers": missing, "unexpected_providers": unexpected},
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
    backup_artifact: Path | None = None,
    thresholds: HealthThresholds | None = None,
    expected_providers: Sequence[str] | None = None,
) -> OperationsHealthReport:
    _require_aware(now, "now")
    thresholds = thresholds or HealthThresholds()
    if evidence.get("schema_version") != "operations-health-evidence-v1":
        raise ValueError("operations evidence schema is unsupported")
    resolved_providers = _resolve_expected_providers(evidence, expected_providers)
    heartbeat = _optional_datetime(evidence.get("worker_heartbeat_at"), "worker_heartbeat_at")
    running_jobs = evidence.get("running_jobs")
    budgets = evidence.get("provider_budgets")
    database_freshness = _database_evidence_freshness(
        evidence.get("collected_at"),
        now=now,
        max_age=thresholds.database_evidence_max_age,
    )
    if database_freshness is None:
        running_jobs_check = evaluate_running_jobs(
            running_jobs if isinstance(running_jobs, list) else None,
            now=now,
            max_age=thresholds.running_job_max_age,
        )
        provider_budgets_check = evaluate_provider_budgets(
            budgets if isinstance(budgets, list) else None,
            expected_providers=resolved_providers,
            warning_ratio=thresholds.budget_warning_ratio,
        )
    else:
        status, summary, freshness_evidence = database_freshness
        running_jobs_check = HealthCheck("running_jobs", status, summary, freshness_evidence)
        provider_budgets_check = HealthCheck(
            "provider_budgets", status, summary, freshness_evidence
        )
    return OperationsHealthReport(
        evaluated_at=now,
        checks=(
            evaluate_heartbeat(heartbeat, now=now, max_age=thresholds.heartbeat_max_age),
            evaluate_backup_manifest(
                backup_manifest,
                now=now,
                max_age=thresholds.backup_max_age,
            ),
            evaluate_backup_artifact(backup_manifest, artifact_path=backup_artifact),
            evaluate_ntp(
                evidence.get("ntp") if isinstance(evidence.get("ntp"), Mapping) else None,
                now=now,
                evidence_max_age=thresholds.ntp_evidence_max_age,
                max_abs_offset_ms=thresholds.ntp_max_abs_offset_ms,
            ),
            running_jobs_check,
            provider_budgets_check,
        ),
    )


def _resolve_expected_providers(
    evidence: Mapping[str, Any], explicit: Sequence[str] | None
) -> tuple[str, ...]:
    raw: object = explicit if explicit is not None else evidence.get("expected_providers")
    if raw is None:
        raw = ("openai", "anthropic", "google")
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise ValueError("expected providers must be an array")
    providers = tuple(raw)
    if any(
        not isinstance(provider, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", provider) is None
        for provider in providers
    ):
        raise ValueError("expected providers contain an invalid identity")
    if len(providers) != len(set(providers)):
        raise ValueError("expected providers must be unique")
    return cast(tuple[str, ...], providers)


def _database_evidence_freshness(
    value: object,
    *,
    now: datetime,
    max_age: timedelta,
) -> tuple[HealthStatus, str, Mapping[str, Any]] | None:
    if value is None:
        return (
            HealthStatus.UNKNOWN,
            "database evidence collection time was not supplied",
            {"evidence_present": False},
        )
    if not isinstance(value, str):
        return (
            HealthStatus.UNKNOWN,
            "database evidence collection time is invalid",
            {"evidence_present": False},
        )
    try:
        collected_at = _parse_datetime(value, "collected_at")
    except ValueError:
        return (
            HealthStatus.UNKNOWN,
            "database evidence collection time is invalid",
            {"evidence_present": False},
        )
    age = now - collected_at
    freshness_evidence = {
        "collected_at": _iso(collected_at),
        "age_seconds": age.total_seconds(),
    }
    if age < timedelta(0):
        return (
            HealthStatus.CRITICAL,
            "database evidence collection time is in the future",
            freshness_evidence,
        )
    if age > max_age:
        return (
            HealthStatus.CRITICAL,
            "database evidence is stale",
            freshness_evidence,
        )
    return None


def _latest_backup_manifest_with_path(
    path: Path | None,
) -> tuple[Mapping[str, Any], Path] | None:
    if path is None:
        return None
    candidates = [path] if path.is_file() else sorted(path.glob("*.manifest.json"))
    manifests: list[tuple[Mapping[str, Any], Path]] = []
    for candidate in candidates:
        document = json.loads(candidate.read_text(encoding="utf-8"))
        if isinstance(document, Mapping):
            manifests.append((document, candidate))
    if not manifests:
        return None
    return max(
        manifests,
        key=lambda item: _optional_datetime(item[0].get("created_at_utc"), "created_at_utc")
        or datetime.min.replace(tzinfo=UTC),
    )


def load_latest_backup_manifest(path: Path | None) -> Mapping[str, Any] | None:
    selected = _latest_backup_manifest_with_path(path)
    return None if selected is None else selected[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate read-only vlytics operations evidence")
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--backup-manifest", type=Path)
    parser.add_argument("--backup-artifacts", type=Path)
    parser.add_argument("--now", help="UTC/offset ISO timestamp; defaults to current UTC")
    parser.add_argument("--heartbeat-max-age-seconds", type=int, default=180)
    parser.add_argument("--backup-max-age-hours", type=int, default=25)
    parser.add_argument("--ntp-evidence-max-age-seconds", type=int, default=600)
    parser.add_argument("--ntp-max-abs-offset-ms", type=float, default=1000.0)
    parser.add_argument("--running-job-max-age-seconds", type=int, default=600)
    parser.add_argument("--database-evidence-max-age-seconds", type=int, default=600)
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
            database_evidence_max_age=timedelta(seconds=args.database_evidence_max_age_seconds),
            budget_warning_ratio=_decimal(args.budget_warning_ratio, "budget_warning_ratio"),
        )
        selected_backup = _latest_backup_manifest_with_path(args.backup_manifest)
        backup_manifest = None if selected_backup is None else selected_backup[0]
        backup_artifact = None
        if selected_backup is not None:
            manifest_path = selected_backup[1]
            if not manifest_path.name.endswith(".manifest.json"):
                raise ValueError("backup manifest filename is invalid")
            artifact_root = args.backup_artifacts or manifest_path.parent
            backup_artifact = artifact_root / manifest_path.name.removesuffix(".manifest.json")
        report = evaluate_operations_health(
            evidence,
            backup_manifest=backup_manifest,
            backup_artifact=backup_artifact,
            now=now,
            thresholds=thresholds,
            expected_providers=args.expected_providers,
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
