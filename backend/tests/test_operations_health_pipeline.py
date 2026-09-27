"""Exercise real evidence evaluation through the optional notification boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from vlytics.ops.health import evaluate_operations_health
from vlytics.ops.health_dispatch import dispatch_health_report, main
from vlytics.ops.health_evidence import collect_operations_evidence


def test_real_missing_evidence_report_dry_run_and_delivery_are_separate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    evidence = collect_operations_evidence(
        database_url=None,
        operational_config_path=tmp_path / "missing.toml",
        operational_schema_path=tmp_path / "missing.schema.json",
        heartbeat_path=tmp_path / "missing-heartbeat.json",
        ntp_evidence_path=tmp_path / "missing-ntp.json",
        now=now,
    )
    report = evaluate_operations_health(evidence, backup_manifest=None, now=now).to_dict()
    assert report["status"] == "unknown"
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    original = path.read_bytes()
    env_name = "VLYTICS_PIPELINE_TEST_WEBHOOK"
    monkeypatch.delenv(env_name, raising=False)
    state = tmp_path / "dispatch.sqlite3"
    assert (
        main(["--report", str(path), "--destination-env", env_name, "--state-db", str(state)]) == 0
    )
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["report_status"] == "unknown"
    assert dry_run["mode"] == "dry_run"
    assert dry_run["planned"] == 5
    assert dry_run["sent"] == 0
    assert not state.exists()

    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    monkeypatch.setenv(env_name, "https://example.invalid/synthetic-health")
    result = dispatch_health_report(
        report,
        destination_env=env_name,
        state_path=state,
        send=True,
        now=now,
        transport=httpx.MockTransport(receive),
    )
    assert result.sent == 5
    assert len(requests) == 5
    assert path.read_bytes() == original
    assert report["notification_dispatched"] is False
    assert {json.loads(request.content)["check"]["check_id"] for request in requests} == {
        "worker_heartbeat",
        "backup_freshness",
        "ntp_sync",
        "running_jobs",
        "provider_budgets",
    }
