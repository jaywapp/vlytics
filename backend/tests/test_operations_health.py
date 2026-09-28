from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from vlytics.ops.health import (
    HealthStatus,
    HealthThresholds,
    evaluate_operations_health,
    load_latest_backup_manifest,
    main,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _manifest(created_at: str = "2026-09-27T11:00:00+00:00") -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "created_at_utc": created_at,
        "preserves_owner_and_acl": True,
        "sha256": "a" * 64,
        "byte_length": 1024,
    }


def _write_backup_fixture(directory: Path) -> Path:
    artifact = directory / "backup"
    content = b"synthetic backup artifact"
    artifact.write_bytes(content)
    manifest = _manifest()
    manifest["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["byte_length"] = len(content)
    manifest_path = directory / "backup.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _budget(provider: str, ratio: str = "0.20") -> dict[str, object]:
    return {
        "provider": provider,
        "daily_used_amount": ratio,
        "daily_limit_amount": "1",
        "daily_used_calls": 2,
        "daily_limit_calls": 10,
        "monthly_used_amount": ratio,
        "monthly_limit_amount": "1",
        "monthly_used_calls": 20,
        "monthly_limit_calls": 100,
    }


def _evidence() -> dict[str, object]:
    return {
        "schema_version": "operations-health-evidence-v1",
        "collected_at": "2026-09-27T11:59:00+00:00",
        "expected_providers": ["openai", "anthropic", "google"],
        "worker_heartbeat_at": "2026-09-27T11:59:00+00:00",
        "ntp": {
            "synchronized": True,
            "observed_at": "2026-09-27T11:58:00+00:00",
            "offset_ms": 12.5,
        },
        "running_jobs": [
            {
                "job_id": "job-1",
                "lease_started_at": "2026-09-27T11:58:00+00:00",
                "lease_until": "2026-09-27T12:03:00+00:00",
            }
        ],
        "provider_budgets": [
            _budget("openai"),
            _budget("anthropic"),
            _budget("google"),
        ],
    }


def _check_statuses(report: object) -> dict[str, HealthStatus]:
    return {check.check_id: check.status for check in report.checks}  # type: ignore[attr-defined]


def test_healthy_evidence_reports_ok_without_claiming_notification_delivery(
    tmp_path: Path,
) -> None:
    manifest_path = _write_backup_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = evaluate_operations_health(
        _evidence(), backup_manifest=manifest, backup_artifact=tmp_path / "backup", now=NOW
    )

    assert report.status is HealthStatus.OK
    assert report.exit_code == 0
    assert set(_check_statuses(report).values()) == {HealthStatus.OK}
    assert report.to_dict()["notification_required"] is False
    assert report.to_dict()["notification_dispatched"] is False


@pytest.mark.parametrize(
    ("mutate", "check_id"),
    [
        (
            lambda evidence: evidence.update({"worker_heartbeat_at": "2026-09-27T11:50:00+00:00"}),
            "worker_heartbeat",
        ),
        (
            lambda evidence: evidence.update(
                {
                    "ntp": {
                        "synchronized": False,
                        "observed_at": "2026-09-27T11:58:00+00:00",
                        "offset_ms": 12.5,
                    }
                }
            ),
            "ntp_sync",
        ),
        (
            lambda evidence: evidence.update(
                {
                    "running_jobs": [
                        {
                            "job_id": "job-1",
                            "lease_started_at": "2026-09-27T11:40:00+00:00",
                            "lease_until": "2026-09-27T11:45:00+00:00",
                        }
                    ]
                }
            ),
            "running_jobs",
        ),
        (
            lambda evidence: evidence.update(
                {
                    "provider_budgets": [
                        _budget("openai", "1.00"),
                        _budget("anthropic"),
                        _budget("google"),
                    ]
                }
            ),
            "provider_budgets",
        ),
    ],
)
def test_critical_operational_evidence_is_actionable(mutate: object, check_id: str) -> None:
    evidence = _evidence()
    mutate(evidence)  # type: ignore[operator]

    report = evaluate_operations_health(evidence, backup_manifest=_manifest(), now=NOW)

    assert _check_statuses(report)[check_id] is HealthStatus.CRITICAL
    assert report.status is HealthStatus.CRITICAL
    assert report.exit_code == 3
    assert report.to_dict()["notification_required"] is True
    assert report.to_dict()["notification_dispatched"] is False


def test_stale_backup_is_critical() -> None:
    report = evaluate_operations_health(
        _evidence(),
        backup_manifest=_manifest("2026-09-26T10:00:00+00:00"),
        now=NOW,
    )

    assert _check_statuses(report)["backup_freshness"] is HealthStatus.CRITICAL


def test_missing_provider_and_fractional_call_usage_are_unknown() -> None:
    missing = _evidence()
    missing["provider_budgets"] = [_budget("openai"), _budget("anthropic")]
    missing_report = evaluate_operations_health(missing, backup_manifest=_manifest(), now=NOW)

    fractional = _evidence()
    fractional_budgets = fractional["provider_budgets"]
    assert isinstance(fractional_budgets, list)
    fractional_budgets[0]["daily_used_calls"] = Decimal("1.5")
    fractional_report = evaluate_operations_health(
        fractional,
        backup_manifest=_manifest(),
        now=NOW,
    )

    assert _check_statuses(missing_report)["provider_budgets"] is HealthStatus.UNKNOWN
    assert _check_statuses(fractional_report)["provider_budgets"] is HealthStatus.UNKNOWN
    assert missing_report.exit_code == 3


def test_budget_warning_uses_distinct_exit_code(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["provider_budgets"] = [
        _budget("openai", "0.80"),
        _budget("anthropic"),
        _budget("google"),
    ]

    manifest_path = _write_backup_fixture(tmp_path)
    report = evaluate_operations_health(
        evidence,
        backup_manifest=json.loads(manifest_path.read_text(encoding="utf-8")),
        backup_artifact=tmp_path / "backup",
        now=NOW,
        thresholds=HealthThresholds(budget_warning_ratio=Decimal("0.80")),
    )

    assert report.status is HealthStatus.WARNING
    assert report.exit_code == 2


def test_unsupported_evidence_schema_is_rejected() -> None:
    evidence = _evidence()
    evidence["schema_version"] = "unknown"

    with pytest.raises(ValueError, match="schema"):
        evaluate_operations_health(evidence, backup_manifest=_manifest(), now=NOW)


def test_latest_backup_manifest_is_selected_by_creation_time(tmp_path: Path) -> None:
    older = _manifest("2026-09-25T12:00:00+00:00")
    newer = _manifest("2026-09-27T11:00:00+00:00")
    (tmp_path / "z-old.manifest.json").write_text(json.dumps(older), encoding="utf-8")
    (tmp_path / "a-new.manifest.json").write_text(json.dumps(newer), encoding="utf-8")

    selected = load_latest_backup_manifest(tmp_path)

    assert selected is not None
    assert selected["created_at_utc"] == newer["created_at_utc"]


def test_cli_emits_machine_readable_report_without_sending_alert(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_path = tmp_path / "evidence.json"
    manifest_path = _write_backup_fixture(tmp_path)
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--backup-manifest",
            str(manifest_path),
            "--now",
            NOW.isoformat(),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "ok"
    assert output["notification_required"] is False
    assert output["notification_dispatched"] is False


@pytest.mark.parametrize(
    ("mutation", "expected_summary"),
    [
        ("missing", "missing or has the wrong size"),
        ("truncated", "missing or has the wrong size"),
        ("changed", "hash does not match"),
    ],
)
def test_backup_artifact_integrity_is_required(
    tmp_path: Path, mutation: str, expected_summary: str
) -> None:
    manifest_path = _write_backup_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = tmp_path / "backup"
    if mutation == "missing":
        artifact.unlink()
    elif mutation == "truncated":
        artifact.write_bytes(artifact.read_bytes()[:-1])
    else:
        content = artifact.read_bytes()
        artifact.write_bytes(b"x" + content[1:])

    report = evaluate_operations_health(
        _evidence(), backup_manifest=manifest, backup_artifact=artifact, now=NOW
    )
    backup = next(check for check in report.checks if check.check_id == "backup_integrity")

    assert backup.status is HealthStatus.CRITICAL
    assert expected_summary in backup.summary
    assert report.exit_code == 3


def test_cli_verifies_artifact_in_separate_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")
    manifest_path = _write_backup_fixture(tmp_path)
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir()
    (tmp_path / "backup").rename(artifact_directory / "backup")

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--backup-manifest",
            str(manifest_path),
            "--backup-artifacts",
            str(artifact_directory),
            "--now",
            NOW.isoformat(),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert (
        next(check for check in output["checks"] if check["check_id"] == "backup_integrity")[
            "status"
        ]
        == "ok"
    )


def test_cli_fails_closed_when_backup_artifact_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")
    manifest_path = _write_backup_fixture(tmp_path)
    (tmp_path / "backup").unlink()

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--backup-manifest",
            str(manifest_path),
            "--now",
            NOW.isoformat(),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert output["status"] == "critical"
    assert (
        next(check for check in output["checks"] if check["check_id"] == "backup_integrity")[
            "status"
        ]
        == "critical"
    )


def test_cli_fails_closed_when_required_evidence_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps({"schema_version": "operations-health-evidence-v1"}),
        encoding="utf-8",
    )

    exit_code = main(["--evidence", str(evidence_path), "--now", NOW.isoformat()])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert output["status"] == "unknown"
    assert output["notification_required"] is True
    assert output["notification_dispatched"] is False


def test_evidence_provider_set_supports_disabled_providers() -> None:
    evidence = _evidence()
    evidence["expected_providers"] = []
    evidence["provider_budgets"] = []

    report = evaluate_operations_health(evidence, backup_manifest=_manifest(), now=NOW)

    assert _check_statuses(report)["provider_budgets"] is HealthStatus.OK


@pytest.mark.parametrize(
    ("collected_at", "expected"),
    [
        (None, HealthStatus.UNKNOWN),
        ("2026-09-27T12:01:00+00:00", HealthStatus.CRITICAL),
        ("2026-09-27T11:40:00+00:00", HealthStatus.CRITICAL),
    ],
)
def test_database_checks_require_fresh_collection_time(
    collected_at: str | None, expected: HealthStatus
) -> None:
    evidence = _evidence()
    if collected_at is None:
        evidence.pop("collected_at")
    else:
        evidence["collected_at"] = collected_at

    report = evaluate_operations_health(evidence, backup_manifest=_manifest(), now=NOW)
    statuses = _check_statuses(report)

    assert statuses["running_jobs"] is expected
    assert statuses["provider_budgets"] is expected


def test_malformed_database_rows_report_unknown_without_traceback() -> None:
    evidence = _evidence()
    evidence["running_jobs"] = [42]
    evidence["provider_budgets"] = [42]

    report = evaluate_operations_health(evidence, backup_manifest=_manifest(), now=NOW)
    statuses = _check_statuses(report)

    assert statuses["running_jobs"] is HealthStatus.UNKNOWN
    assert statuses["provider_budgets"] is HealthStatus.UNKNOWN


def test_cli_explicit_provider_overrides_collector_provider_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = _evidence()
    evidence["expected_providers"] = []
    evidence["provider_budgets"] = [_budget("openai")]
    evidence_path = tmp_path / "evidence.json"
    manifest_path = _write_backup_fixture(tmp_path)
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    exit_code = main(
        [
            "--evidence",
            str(evidence_path),
            "--backup-manifest",
            str(manifest_path),
            "--expected-provider",
            "openai",
            "--now",
            NOW.isoformat(),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "ok"
