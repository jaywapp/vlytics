"""Generic health webhook dispatch remains sanitized, deduplicated, and fail closed."""

from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from vlytics.ops.health_dispatch import DispatchError, DispatchResult, dispatch_health_report, main

DESTINATION_ENV = "VLYTICS_TEST_HEALTH_WEBHOOK_URL"
DESTINATION = "https://hooks.example.invalid/private-token"
NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
CHECK_IDS = (
    "worker_heartbeat",
    "backup_freshness",
    "ntp_sync",
    "running_jobs",
    "provider_budgets",
)
PRIORITY = {"ok": 0, "warning": 1, "unknown": 2, "critical": 3}


def _report(
    overrides: dict[str, tuple[str, dict[str, object]]] | None = None,
    *,
    evaluated_at: str = "2026-09-27T12:00:00+00:00",
) -> dict[str, Any]:
    selected = overrides or {}
    defaults: dict[str, dict[str, object]] = {
        "worker_heartbeat": {
            "heartbeat_at": "2026-09-27T11:59:00+00:00",
            "age_seconds": 60.0,
        },
        "backup_freshness": {
            "created_at_utc": "2026-09-27T10:00:00+00:00",
            "age_seconds": 7200.0,
        },
        "ntp_sync": {
            "observed_at": "2026-09-27T11:59:30+00:00",
            "age_seconds": 30.0,
        },
        "running_jobs": {"running_job_count": 0},
        "provider_budgets": {"providers": {}, "warning_ratio": "0.80"},
    }
    checks: list[dict[str, object]] = []
    for check_id in CHECK_IDS:
        status, evidence = selected.get(check_id, ("ok", defaults[check_id]))
        checks.append(
            {
                "check_id": check_id,
                "status": status,
                "summary": f"private summary for {check_id}",
                "evidence": evidence,
            }
        )
    status = max((str(check["status"]) for check in checks), key=PRIORITY.__getitem__)
    return {
        "schema_version": "operations-health-report-v1",
        "evaluated_at": evaluated_at,
        "status": status,
        "notification_required": status != "ok",
        "notification_dispatched": False,
        "checks": checks,
    }


def _critical_report(
    *,
    age_seconds: float = 600.0,
    evaluated_at: str = "2026-09-27T12:00:00+00:00",
) -> dict[str, Any]:
    return _report(
        {
            "worker_heartbeat": (
                "critical",
                {
                    "heartbeat_at": "2026-09-27T11:50:00+00:00",
                    "age_seconds": age_seconds,
                },
            )
        },
        evaluated_at=evaluated_at,
    )


def _success_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    return httpx.MockTransport(handler)


def test_dry_run_never_reads_destination_sends_or_consumes_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DESTINATION_ENV, raising=False)
    requests: list[httpx.Request] = []
    state = tmp_path / "state.sqlite3"

    result = dispatch_health_report(
        _critical_report(),
        destination_env=DESTINATION_ENV,
        state_path=state,
        transport=_success_transport(requests),
    )

    assert result == DispatchResult("critical", "dry_run", 1)
    assert requests == []
    assert not state.exists()


def test_payload_is_allowlisted_and_request_has_stable_idempotency_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    requests: list[httpx.Request] = []
    result = dispatch_health_report(
        _critical_report(),
        destination_env=DESTINATION_ENV,
        state_path=tmp_path / "state.sqlite3",
        send=True,
        transport=_success_transport(requests),
        now=NOW,
    )

    assert result.sent == 1
    payload = json.loads(requests[0].content)
    assert set(payload) == {
        "schema_version",
        "channel",
        "report_status",
        "evaluated_at",
        "check",
        "idempotency_key",
    }
    assert payload["channel"] == "generic_webhook_v1"
    assert payload["check"] == {
        "check_id": "worker_heartbeat",
        "status": "critical",
        "evidence_timestamps": ["2026-09-27T11:50:00+00:00"],
    }
    assert requests[0].headers["Idempotency-Key"] == payload["idempotency_key"]
    serialized = json.dumps(payload)
    assert "private summary" not in serialized
    assert "age_seconds" not in serialized
    assert "private-token" not in serialized


def test_restart_suppresses_age_only_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    requests: list[httpx.Request] = []
    state = tmp_path / "state.sqlite3"
    first = dispatch_health_report(
        _critical_report(),
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        transport=_success_transport(requests),
        now=NOW,
    )
    second = dispatch_health_report(
        _critical_report(age_seconds=900, evaluated_at="2026-09-27T12:05:00+00:00"),
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        transport=_success_transport(requests),
        now=NOW,
    )

    assert first.sent == 1
    assert second.suppressed == 1
    assert len(requests) == 1


def test_recovery_allows_same_incident_to_alert_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    requests: list[httpx.Request] = []
    state = tmp_path / "state.sqlite3"
    for report in (
        _critical_report(evaluated_at="2026-09-27T12:00:00+00:00"),
        _report(evaluated_at="2026-09-27T12:01:00+00:00"),
        _critical_report(evaluated_at="2026-09-27T12:02:00+00:00"),
    ):
        result = dispatch_health_report(
            report,
            destination_env=DESTINATION_ENV,
            state_path=state,
            send=True,
            transport=_success_transport(requests),
            now=NOW,
        )
        assert result.failed == 0
    assert len(requests) == 2
    keys = [request.headers["Idempotency-Key"] for request in requests]
    assert keys[0] != keys[1]


def test_http_failure_retries_same_key_without_mutating_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    report = _critical_report()
    original = copy.deepcopy(report)
    statuses = iter((503, 204))
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(next(statuses))

    state = tmp_path / "state.sqlite3"
    failed = dispatch_health_report(
        report,
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        transport=httpx.MockTransport(handler),
        now=NOW,
    )
    retried = dispatch_health_report(
        report,
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        transport=httpx.MockTransport(handler),
        now=NOW,
    )

    assert failed.exit_code == 3
    assert retried.sent == 1
    assert keys[0] == keys[1]
    assert report == original


def test_redirect_is_failure_and_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid/secret"})

    result = dispatch_health_report(
        _critical_report(),
        destination_env=DESTINATION_ENV,
        state_path=tmp_path / "state.sqlite3",
        send=True,
        transport=httpx.MockTransport(handler),
        now=NOW,
    )
    assert result.failed == 1
    assert len(requests) == 1


def test_destination_hash_namespaces_deduplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    state = tmp_path / "state.sqlite3"
    for destination in (DESTINATION, "https://second.example.invalid/other-token"):
        monkeypatch.setenv(DESTINATION_ENV, destination)
        result = dispatch_health_report(
            _critical_report(),
            destination_env=DESTINATION_ENV,
            state_path=state,
            send=True,
            transport=_success_transport(requests),
            now=NOW,
        )
        assert result.sent == 1
    assert len(requests) == 2


def test_concurrent_dispatchers_reserve_one_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    state = tmp_path / "state.sqlite3"
    dispatch_health_report(
        _report(evaluated_at="2026-09-27T11:59:00+00:00"),
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        now=NOW,
    )
    requests: list[httpx.Request] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def invoke() -> DispatchResult:
        barrier.wait()

        def handler(request: httpx.Request) -> httpx.Response:
            with lock:
                requests.append(request)
            return httpx.Response(200)

        return dispatch_health_report(
            _critical_report(),
            destination_env=DESTINATION_ENV,
            state_path=state,
            send=True,
            transport=httpx.MockTransport(handler),
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(), range(2)))

    assert sum(result.sent for result in results) == 1
    assert sum(result.suppressed for result in results) == 1
    assert len(requests) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        ("schema_version", "unsupported"),
        ("status", "ok"),
        ("notification_dispatched", True),
        ("evaluated_at", "2026-09-27T12:00:00"),
    ],
)
def test_invalid_or_inconsistent_report_fields_fail_closed(
    mutation: tuple[str, object],
) -> None:
    report = _critical_report()
    report[mutation[0]] = mutation[1]
    with pytest.raises(DispatchError):
        dispatch_health_report(report, destination_env=DESTINATION_ENV, state_path=None)


def test_invalid_checks_and_evidence_timestamps_fail_closed() -> None:
    missing = _critical_report()
    cast(list[object], missing["checks"]).pop()
    with pytest.raises(DispatchError):
        dispatch_health_report(missing, destination_env=DESTINATION_ENV, state_path=None)

    invalid_time = _critical_report()
    checks = cast(list[dict[str, Any]], invalid_time["checks"])
    evidence = cast(dict[str, object], checks[0]["evidence"])
    evidence["heartbeat_at"] = "not-a-time"
    with pytest.raises(DispatchError):
        dispatch_health_report(invalid_time, destination_env=DESTINATION_ENV, state_path=None)


def test_cli_dry_run_does_not_require_config_or_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(DESTINATION_ENV, raising=False)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_critical_report()), encoding="utf-8")
    state = tmp_path / "state.sqlite3"
    exit_code = main(
        [
            "--report",
            str(report_path),
            "--destination-env",
            DESTINATION_ENV,
            "--state-db",
            str(state),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["mode"] == "dry_run"
    assert output["planned"] == 1
    assert not state.exists()


def test_cli_send_disabled_by_minimal_operational_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(DESTINATION_ENV, raising=False)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_critical_report()), encoding="utf-8")
    config = tmp_path / "operational.toml"
    config.write_text(
        "[unrelated]\n"
        'value = "ignored"\n\n'
        "[alerting]\n"
        "enabled = false\n"
        'channel = ""\n'
        'destination_env = "VLYTICS_TEST_HEALTH_WEBHOOK_URL"\n',
        encoding="utf-8",
    )
    state = tmp_path / "state.sqlite3"
    exit_code = main(
        [
            "--report",
            str(report_path),
            "--destination-env",
            DESTINATION_ENV,
            "--state-db",
            str(state),
            "--operational-config",
            str(config),
            "--send",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["mode"] == "disabled"
    assert not state.exists()


def test_cli_send_requires_matching_enabled_alerting_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_critical_report()), encoding="utf-8")
    config = tmp_path / "operational.toml"
    config.write_text(
        "[alerting]\n"
        "enabled = true\n"
        'channel = "different"\n'
        'destination_env = "VLYTICS_TEST_HEALTH_WEBHOOK_URL"\n',
        encoding="utf-8",
    )
    exit_code = main(
        [
            "--report",
            str(report_path),
            "--destination-env",
            DESTINATION_ENV,
            "--state-db",
            str(tmp_path / "state.sqlite3"),
            "--operational-config",
            str(config),
            "--send",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert output["error_code"] == "alerting_config_mismatch"
    assert DESTINATION not in json.dumps(output)


def test_older_recovery_cannot_clear_a_newer_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    requests: list[httpx.Request] = []
    state = tmp_path / "state.sqlite3"

    first = dispatch_health_report(
        _critical_report(evaluated_at="2026-09-27T12:10:00+00:00"),
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        transport=_success_transport(requests),
        now=NOW,
    )
    stale_recovery = dispatch_health_report(
        _report(evaluated_at="2026-09-27T12:05:00+00:00"),
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        transport=_success_transport(requests),
        now=NOW,
    )
    repeated = dispatch_health_report(
        _critical_report(age_seconds=900, evaluated_at="2026-09-27T12:15:00+00:00"),
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        transport=_success_transport(requests),
        now=NOW,
    )

    assert first.sent == 1
    assert stale_recovery.sent == 0
    assert repeated.suppressed == 1
    assert len(requests) == 1


def test_concurrent_older_recovery_cannot_overwrite_reserved_newer_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    state = tmp_path / "state.sqlite3"
    entered = threading.Event()
    release = threading.Event()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entered.set()
        assert release.wait(timeout=5)
        return httpx.Response(204)

    def send_newer_fault() -> DispatchResult:
        return dispatch_health_report(
            _critical_report(evaluated_at="2026-09-27T12:10:00+00:00"),
            destination_env=DESTINATION_ENV,
            state_path=state,
            send=True,
            transport=httpx.MockTransport(handler),
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(send_newer_fault)
        assert entered.wait(timeout=5)
        stale = dispatch_health_report(
            _report(evaluated_at="2026-09-27T12:05:00+00:00"),
            destination_env=DESTINATION_ENV,
            state_path=state,
            send=True,
            transport=_success_transport(requests),
            now=NOW,
        )
        release.set()
        newer = pending.result(timeout=5)

    repeated = dispatch_health_report(
        _critical_report(age_seconds=900, evaluated_at="2026-09-27T12:15:00+00:00"),
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        transport=_success_transport(requests),
        now=NOW,
    )
    assert stale.sent == 0
    assert newer.sent == 1
    assert repeated.suppressed == 1
    assert len(requests) == 1


def test_each_sequential_alert_reserves_from_its_actual_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    state = tmp_path / "state.sqlite3"
    current = [NOW]
    leases: list[float] = []

    def clock() -> datetime:
        return current[0]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        check_id = payload["check"]["check_id"]
        import sqlite3

        with sqlite3.connect(state) as connection:
            row = connection.execute(
                "SELECT reserved_until FROM health_dispatch_state WHERE check_id = ?",
                (check_id,),
            ).fetchone()
        assert row is not None
        leases.append(float(row[0]))
        if len(leases) == 1:
            current[0] += timedelta(seconds=90)
        return httpx.Response(204)

    report = _report(
        {
            "worker_heartbeat": (
                "critical",
                {
                    "heartbeat_at": "2026-09-27T11:50:00+00:00",
                    "age_seconds": 600,
                },
            ),
            "backup_freshness": (
                "warning",
                {
                    "created_at_utc": "2026-09-27T10:00:00+00:00",
                    "age_seconds": 7200,
                },
            ),
        }
    )
    result = dispatch_health_report(
        report,
        destination_env=DESTINATION_ENV,
        state_path=state,
        send=True,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
        clock=clock,
    )

    assert result.sent == 2
    assert leases[0] == (NOW + timedelta(seconds=40)).timestamp()
    assert leases[1] == (NOW + timedelta(seconds=130)).timestamp()


def test_success_status_does_not_materialize_response_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    iterated = False

    class UnreadableStream(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            nonlocal iterated
            iterated = True
            raise AssertionError("response body must not be read")

    transport = httpx.MockTransport(lambda _: httpx.Response(204, stream=UnreadableStream()))
    result = dispatch_health_report(
        _critical_report(),
        destination_env=DESTINATION_ENV,
        state_path=tmp_path / "state.sqlite3",
        send=True,
        transport=transport,
        now=NOW,
    )

    assert result.sent == 1
    assert not iterated


def test_returning_to_prior_non_ok_observation_uses_a_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DESTINATION_ENV, DESTINATION)
    requests: list[httpx.Request] = []
    state = tmp_path / "state.sqlite3"
    reports = (
        _critical_report(evaluated_at="2026-09-27T12:00:00+00:00"),
        _report(
            {
                "worker_heartbeat": (
                    "warning",
                    {
                        "heartbeat_at": "2026-09-27T11:55:00+00:00",
                        "age_seconds": 360,
                    },
                )
            },
            evaluated_at="2026-09-27T12:01:00+00:00",
        ),
        _critical_report(evaluated_at="2026-09-27T12:02:00+00:00"),
    )

    results = [
        dispatch_health_report(
            report,
            destination_env=DESTINATION_ENV,
            state_path=state,
            send=True,
            transport=_success_transport(requests),
            now=NOW,
        )
        for report in reports
    ]

    assert [result.sent for result in results] == [1, 1, 1]
    keys = [request.headers["Idempotency-Key"] for request in requests]
    assert len(set(keys)) == 3
