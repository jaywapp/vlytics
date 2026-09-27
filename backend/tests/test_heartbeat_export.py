from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vlytics.ops import heartbeat_export
from vlytics.ops.health import HealthStatus, evaluate_operations_health

NOW = datetime(2026, 9, 27, 4, tzinfo=UTC)


def _heartbeat(**changes: object) -> dict[str, object]:
    return {
        "schema_version": "worker-heartbeat-v1",
        "completed_at": NOW.isoformat(),
        "processed": 2,
        "pid": 123,
        **changes,
    }


def test_export_reads_only_known_file_and_drops_extra_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 10
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(
            command, 0, json.dumps(_heartbeat(secret="must-not-export")).encode(), b""
        )

    monkeypatch.setattr(heartbeat_export.subprocess, "run", run)
    output = tmp_path / "health" / "heartbeat.json"
    result = heartbeat_export.export_worker_heartbeat(
        container="vlytics-worker-1", output=output, now=NOW
    )
    assert result["exported"] is True
    assert json.loads(output.read_text()) == _heartbeat()
    assert commands[0][:5] == ["docker", "exec", "vlytics-worker-1", "python", "-c"]
    assert commands[0][-1] == heartbeat_export.DEFAULT_PATH
    assert not list(output.parent.glob(".heartbeat-*"))


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"x" * (heartbeat_export.MAX_BYTES + 1),
        json.dumps(_heartbeat(completed_at="2026-09-27T04:00:00")).encode(),
        json.dumps(_heartbeat(completed_at=(NOW + timedelta(seconds=1)).isoformat())).encode(),
        json.dumps(_heartbeat(completed_at=(NOW - timedelta(seconds=181)).isoformat())).encode(),
        json.dumps(_heartbeat(processed=True)).encode(),
        json.dumps(_heartbeat(pid=0)).encode(),
    ],
)
def test_bad_source_replaces_previous_good_copy_with_unavailable_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: bytes
) -> None:
    monkeypatch.setattr(
        heartbeat_export.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, raw, b""),
    )
    output = tmp_path / "heartbeat.json"
    output.write_text(json.dumps(_heartbeat()))
    result = heartbeat_export.export_worker_heartbeat(container="worker", output=output, now=NOW)
    assert result["code"] == "source_invalid"
    value = json.loads(output.read_text())
    assert value["schema_version"] == "worker-heartbeat-export-failure-v1"
    assert "completed_at" not in value


def test_source_failure_is_sanitized_and_does_not_keep_fresh_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, "secret-command", stderr=b"secret-stderr")

    monkeypatch.setattr(heartbeat_export.subprocess, "run", fail)
    output = tmp_path / "heartbeat.json"
    assert heartbeat_export.main(["--container", "worker", "--output", str(output)]) == 3
    assert "secret" not in capsys.readouterr().out
    assert json.loads(output.read_text())["code"] == "source_unavailable"


def test_invalid_container_never_executes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid container must not execute")

    monkeypatch.setattr(heartbeat_export.subprocess, "run", fail)
    result = heartbeat_export.export_worker_heartbeat(
        container="--privileged", output=tmp_path / "heartbeat.json", now=NOW
    )
    assert result["code"] == "source_invalid"


def test_exported_heartbeat_is_consumed_by_existing_health_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vlytics.ops.health_evidence import collect_operations_evidence

    monkeypatch.setattr(
        heartbeat_export.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(_heartbeat()).encode(), b""
        ),
    )
    output = tmp_path / "heartbeat.json"
    heartbeat_export.export_worker_heartbeat(container="worker", output=output, now=NOW)
    evidence = collect_operations_evidence(
        database_url=None,
        operational_config_path=tmp_path / "absent.toml",
        operational_schema_path=tmp_path / "absent.schema.json",
        heartbeat_path=output,
        ntp_evidence_path=tmp_path / "absent-ntp.json",
        now=NOW,
    )
    report = evaluate_operations_health(evidence, backup_manifest=None, now=NOW)
    heartbeat = next(check for check in report.checks if check.check_id == "worker_heartbeat")
    assert heartbeat.status is HealthStatus.OK
    assert report.status is HealthStatus.UNKNOWN


def test_export_uses_completion_clock_for_heartbeat_written_during_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    times = iter((NOW, NOW + timedelta(seconds=2)))

    class ReadClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return next(times)

    monkeypatch.setattr(heartbeat_export, "datetime", ReadClock)
    monkeypatch.setattr(
        heartbeat_export.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(_heartbeat(completed_at=(NOW + timedelta(seconds=1)).isoformat())).encode(),
            b"",
        ),
    )
    output = tmp_path / "heartbeat.json"
    result = heartbeat_export.export_worker_heartbeat(container="worker", output=output)
    assert result["exported"] is True
    assert (
        json.loads(output.read_text())["completed_at"] == (NOW + timedelta(seconds=1)).isoformat()
    )


def test_timeout_marker_records_failure_completion_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    times = iter((NOW, NOW + timedelta(seconds=10)))

    class TimeoutClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return next(times)

    def timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("hidden-runtime-command", 10)

    monkeypatch.setattr(heartbeat_export, "datetime", TimeoutClock)
    monkeypatch.setattr(heartbeat_export.subprocess, "run", timeout)
    output = tmp_path / "heartbeat.json"
    result = heartbeat_export.export_worker_heartbeat(container="worker", output=output)
    assert result["code"] == "source_unavailable"
    assert (
        json.loads(output.read_text())["observed_at"] == (NOW + timedelta(seconds=10)).isoformat()
    )
