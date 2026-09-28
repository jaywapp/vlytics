"""Dispatch sanitized operational health alerts to a generic HTTPS webhook."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

REPORT_SCHEMA = "operations-health-report-v1"
WEBHOOK_SCHEMA = "operations-health-webhook-v1"
RESULT_SCHEMA = "operations-health-dispatch-result-v1"
CHANNEL = "generic_webhook_v1"
CHECK_IDS = frozenset(
    {
        "worker_heartbeat",
        "backup_freshness",
        "backup_integrity",
        "ntp_sync",
        "running_jobs",
        "provider_budgets",
    }
)
STATUSES = frozenset({"ok", "warning", "critical", "unknown"})
STATUS_PRIORITY = {"ok": 0, "warning": 1, "unknown": 2, "critical": 3}
TIMESTAMP_KEYS = frozenset(
    {
        "heartbeat_at",
        "created_at_utc",
        "observed_at",
        "collected_at",
        "lease_started_at",
        "lease_until",
    }
)
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,127}")


class DispatchError(ValueError):
    """A sanitized dispatcher contract error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ValidatedCheck:
    check_id: str
    status: str
    stable_evidence: Mapping[str, Any]
    evidence_timestamps: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedReport:
    evaluated_at: str
    evaluated_at_datetime: datetime
    status: str
    checks: tuple[ValidatedCheck, ...]


@dataclass(frozen=True)
class DispatchResult:
    report_status: str
    mode: str
    planned: int
    sent: int = 0
    suppressed: int = 0
    failed: int = 0

    @property
    def exit_code(self) -> int:
        return 0 if self.failed == 0 else 3

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESULT_SCHEMA,
            "channel": CHANNEL,
            "mode": self.mode,
            "report_status": self.report_status,
            "planned": self.planned,
            "sent": self.sent,
            "suppressed": self.suppressed,
            "failed": self.failed,
        }


def dispatch_health_report(
    report: Mapping[str, Any],
    *,
    destination_env: str,
    state_path: Path | None,
    send: bool = False,
    timeout_seconds: float = 5.0,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DispatchResult:
    """Validate a health report and optionally deliver each changed non-OK check."""

    _validate_environment_name(destination_env)
    validated = _validate_report(report)
    alerts = tuple(check for check in validated.checks if check.status != "ok")
    if not send:
        return DispatchResult(validated.status, "dry_run", len(alerts))
    if state_path is None:
        raise DispatchError("state_path_required")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
        raise DispatchError("invalid_timeout")
    destination = _destination_url(destination_env)
    destination_hash = hashlib.sha256(destination.encode("utf-8")).hexdigest()
    if now is not None and clock is not None:
        raise DispatchError("invalid_dispatch_time")
    if now is not None:
        _dispatch_time(fallback=now)

    try:
        connection = _open_state(state_path)
    except (OSError, sqlite3.Error) as error:
        raise DispatchError("state_unavailable") from error
    sent = 0
    suppressed = 0
    failed = 0
    try:
        for check in validated.checks:
            if check.status == "ok":
                _record_recovery(
                    connection,
                    destination_hash,
                    check.check_id,
                    validated.evaluated_at_datetime,
                )
        try:
            client = httpx.Client(
                transport=transport,
                verify=True,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(timeout_seconds),
            )
        except Exception as error:
            raise DispatchError("webhook_client_unavailable") from error
        with client:
            for check in alerts:
                fingerprint = _fingerprint(destination_hash, check)
                reservation_token = uuid4().hex
                reservation_time = _dispatch_time(clock, fallback=now)
                episode = _reserve(
                    connection,
                    destination_hash=destination_hash,
                    check=check,
                    fingerprint=fingerprint,
                    reservation_token=reservation_token,
                    evaluated_at=validated.evaluated_at_datetime,
                    now=reservation_time,
                    timeout_seconds=timeout_seconds,
                )
                if episode is None:
                    suppressed += 1
                    continue
                idempotency_key = _idempotency_key(fingerprint, episode)
                payload = _payload(validated, check, idempotency_key)
                delivered = False
                try:
                    with client.stream(
                        "POST",
                        destination,
                        json=payload,
                        headers={"Idempotency-Key": idempotency_key},
                    ) as response:
                        delivered = 200 <= response.status_code < 300
                except Exception:
                    delivered = False
                if not delivered:
                    _release_reservation(
                        connection,
                        destination_hash=destination_hash,
                        check_id=check.check_id,
                        reservation_token=reservation_token,
                    )
                    failed += 1
                    continue
                try:
                    _mark_sent(
                        connection,
                        destination_hash=destination_hash,
                        check_id=check.check_id,
                        reservation_token=reservation_token,
                        sent_at=_dispatch_time(clock, fallback=now),
                    )
                except DispatchError:
                    failed += 1
                    continue
                sent += 1
    finally:
        connection.close()
    return DispatchResult(validated.status, "send", len(alerts), sent, suppressed, failed)


def _validate_report(report: Mapping[str, Any]) -> ValidatedReport:
    if set(report) != {
        "schema_version",
        "evaluated_at",
        "status",
        "notification_required",
        "notification_dispatched",
        "checks",
    }:
        raise DispatchError("invalid_health_report")
    if report.get("schema_version") != REPORT_SCHEMA:
        raise DispatchError("unsupported_health_report_schema")
    evaluated_at = _timestamp(report.get("evaluated_at"))
    evaluated_at_datetime = datetime.fromisoformat(evaluated_at)
    status = report.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise DispatchError("invalid_health_report_status")
    checks_value = report.get("checks")
    if not isinstance(checks_value, list) or len(checks_value) != len(CHECK_IDS):
        raise DispatchError("invalid_health_checks")
    checks: list[ValidatedCheck] = []
    seen: set[str] = set()
    for value in checks_value:
        if not isinstance(value, Mapping) or set(value) != {
            "check_id",
            "status",
            "summary",
            "evidence",
        }:
            raise DispatchError("invalid_health_check")
        check_id = value.get("check_id")
        check_status = value.get("status")
        summary = value.get("summary")
        evidence = value.get("evidence")
        if (
            not isinstance(check_id, str)
            or check_id not in CHECK_IDS
            or check_id in seen
            or not isinstance(check_status, str)
            or check_status not in STATUSES
            or not isinstance(summary, str)
            or not summary
            or not isinstance(evidence, Mapping)
        ):
            raise DispatchError("invalid_health_check")
        seen.add(check_id)
        typed_evidence = cast(Mapping[str, Any], evidence)
        _validate_json_value(typed_evidence)
        checks.append(
            ValidatedCheck(
                check_id=check_id,
                status=check_status,
                stable_evidence=cast(Mapping[str, Any], _without_volatile_ages(typed_evidence)),
                evidence_timestamps=tuple(sorted(set(_evidence_timestamps(typed_evidence)))),
            )
        )
    if seen != CHECK_IDS:
        raise DispatchError("invalid_health_checks")
    derived_status = max((check.status for check in checks), key=STATUS_PRIORITY.__getitem__)
    required = report.get("notification_required")
    dispatched = report.get("notification_dispatched")
    if (
        derived_status != status
        or not isinstance(required, bool)
        or required != (status != "ok")
        or dispatched is not False
    ):
        raise DispatchError("inconsistent_health_report")
    return ValidatedReport(evaluated_at, evaluated_at_datetime, status, tuple(checks))


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DispatchError("invalid_health_evidence")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DispatchError("invalid_health_evidence")
            _validate_json_value(item)
        return
    raise DispatchError("invalid_health_evidence")


def _without_volatile_ages(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _without_volatile_ages(item)
            for key, item in value.items()
            if key != "age_seconds"
        }
    if isinstance(value, list):
        return [_without_volatile_ages(item) for item in value]
    return value


def _evidence_timestamps(value: object) -> list[str]:
    timestamps: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in TIMESTAMP_KEYS:
                timestamps.append(_timestamp(item))
            else:
                timestamps.extend(_evidence_timestamps(item))
    elif isinstance(value, list):
        for item in value:
            timestamps.extend(_evidence_timestamps(item))
    return timestamps


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DispatchError("invalid_health_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DispatchError("invalid_health_timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DispatchError("invalid_health_timestamp")
    return parsed.astimezone(UTC).isoformat()


def _validate_environment_name(name: str) -> None:
    if ENV_NAME.fullmatch(name) is None:
        raise DispatchError("invalid_destination_environment")


def _destination_url(environment_name: str) -> str:
    destination = os.environ.get(environment_name)
    if (
        not destination
        or len(destination) > 2048
        or any(character.isspace() for character in destination)
    ):
        raise DispatchError("destination_unavailable")
    try:
        parsed = urlsplit(destination)
        port = parsed.port
    except ValueError as error:
        raise DispatchError("invalid_destination") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise DispatchError("invalid_destination")
    return destination


def _canonical(value: object) -> bytes:
    try:
        serialized = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise DispatchError("invalid_health_evidence") from error
    return serialized.encode("utf-8")


def _fingerprint(destination_hash: str, check: ValidatedCheck) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "channel": CHANNEL,
                "destination_hash": destination_hash,
                "check_id": check.check_id,
                "status": check.status,
                "stable_evidence": check.stable_evidence,
            }
        )
    ).hexdigest()


def _payload(
    report: ValidatedReport, check: ValidatedCheck, idempotency_key: str
) -> dict[str, object]:
    return {
        "schema_version": WEBHOOK_SCHEMA,
        "channel": CHANNEL,
        "report_status": report.status,
        "evaluated_at": report.evaluated_at,
        "check": {
            "check_id": check.check_id,
            "status": check.status,
            "evidence_timestamps": list(check.evidence_timestamps),
        },
        "idempotency_key": idempotency_key,
    }


def _dispatch_time(
    clock: Callable[[], datetime] | None = None,
    *,
    fallback: datetime | None = None,
) -> datetime:
    current = clock() if clock is not None else fallback or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise DispatchError("invalid_dispatch_time")
    return current.astimezone(UTC)


def _idempotency_key(fingerprint: str, episode: int) -> str:
    digest = hashlib.sha256(
        _canonical({"fingerprint": fingerprint, "episode": episode})
    ).hexdigest()
    return f"vlytics-health-{digest}"


def _open_state(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS health_dispatch_state (
            destination_hash TEXT NOT NULL,
            check_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK (disposition IN ('pending', 'sent')),
            reservation_token TEXT,
            reserved_until REAL,
            sent_at TEXT,
            episode INTEGER NOT NULL DEFAULT 0,
            latest_evaluated_at REAL NOT NULL DEFAULT 0,
            health_status TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (destination_hash, check_id)
        )
        """
    )

    return connection


def _record_recovery(
    connection: sqlite3.Connection,
    destination_hash: str,
    check_id: str,
    evaluated_at: datetime,
) -> bool:
    evaluated_epoch = evaluated_at.timestamp()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT episode, latest_evaluated_at
            FROM health_dispatch_state
            WHERE destination_hash = ? AND check_id = ?
            """,
            (destination_hash, check_id),
        ).fetchone()
        if row is not None and evaluated_epoch <= float(row[1]):
            connection.execute("COMMIT")
            return False
        episode = int(row[0]) if row is not None else 0
        connection.execute(
            """
            INSERT INTO health_dispatch_state (
                destination_hash, check_id, fingerprint, disposition,
                reservation_token, reserved_until, sent_at, episode,
                latest_evaluated_at, health_status
            ) VALUES (?, ?, '', 'sent', NULL, NULL, NULL, ?, ?, 'ok')
            ON CONFLICT(destination_hash, check_id) DO UPDATE SET
                fingerprint = '',
                disposition = 'sent',
                reservation_token = NULL,
                reserved_until = NULL,
                sent_at = NULL,
                episode = excluded.episode,
                latest_evaluated_at = excluded.latest_evaluated_at,
                health_status = 'ok'
            """,
            (destination_hash, check_id, episode, evaluated_epoch),
        )
        connection.execute("COMMIT")
        return True
    except sqlite3.Error as error:
        _rollback(connection)
        raise DispatchError("state_unavailable") from error


def _reserve(
    connection: sqlite3.Connection,
    *,
    destination_hash: str,
    check: ValidatedCheck,
    fingerprint: str,
    reservation_token: str,
    evaluated_at: datetime,
    now: datetime,
    timeout_seconds: float,
) -> int | None:
    evaluated_epoch = evaluated_at.timestamp()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT fingerprint, disposition, reserved_until, episode,
                   latest_evaluated_at, health_status
            FROM health_dispatch_state
            WHERE destination_hash = ? AND check_id = ?
            """,
            (destination_hash, check.check_id),
        ).fetchone()
        episode = 1
        if row is not None:
            latest_evaluated_at = float(row[4])
            if evaluated_epoch < latest_evaluated_at:
                connection.execute("COMMIT")
                return None
            episode = int(row[3])
            same_observation = row[0] == fingerprint and row[5] == check.status
            if evaluated_epoch == latest_evaluated_at and not same_observation:
                connection.execute("COMMIT")
                return None
            active_reservation = (
                row[1] == "pending"
                and isinstance(row[2], int | float)
                and float(row[2]) > now.timestamp()
            )
            if same_observation and (row[1] == "sent" or active_reservation):
                if evaluated_epoch > latest_evaluated_at:
                    connection.execute(
                        """
                        UPDATE health_dispatch_state
                        SET latest_evaluated_at = ?
                        WHERE destination_hash = ? AND check_id = ?
                        """,
                        (evaluated_epoch, destination_hash, check.check_id),
                    )
                connection.execute("COMMIT")
                return None
            if evaluated_epoch > latest_evaluated_at and not same_observation:
                episode += 1
            episode = max(1, episode)
        reserved_until = (now + timedelta(seconds=timeout_seconds * 2 + 30)).timestamp()
        connection.execute(
            """
            INSERT INTO health_dispatch_state (
                destination_hash, check_id, fingerprint, disposition,
                reservation_token, reserved_until, sent_at, episode,
                latest_evaluated_at, health_status
            ) VALUES (?, ?, ?, 'pending', ?, ?, NULL, ?, ?, ?)
            ON CONFLICT(destination_hash, check_id) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                disposition = 'pending',
                reservation_token = excluded.reservation_token,
                reserved_until = excluded.reserved_until,
                sent_at = NULL,
                episode = excluded.episode,
                latest_evaluated_at = excluded.latest_evaluated_at,
                health_status = excluded.health_status
            """,
            (
                destination_hash,
                check.check_id,
                fingerprint,
                reservation_token,
                reserved_until,
                episode,
                evaluated_epoch,
                check.status,
            ),
        )
        connection.execute("COMMIT")
        return episode
    except sqlite3.Error as error:
        _rollback(connection)
        raise DispatchError("state_unavailable") from error


def _release_reservation(
    connection: sqlite3.Connection,
    *,
    destination_hash: str,
    check_id: str,
    reservation_token: str,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE health_dispatch_state
            SET reservation_token = NULL, reserved_until = NULL
            WHERE destination_hash = ? AND check_id = ?
              AND disposition = 'pending' AND reservation_token = ?
            """,
            (destination_hash, check_id, reservation_token),
        )
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        _rollback(connection)
        raise DispatchError("state_unavailable") from error


def _mark_sent(
    connection: sqlite3.Connection,
    *,
    destination_hash: str,
    check_id: str,
    reservation_token: str,
    sent_at: datetime,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE health_dispatch_state
            SET disposition = 'sent', reservation_token = NULL,
                reserved_until = NULL, sent_at = ?
            WHERE destination_hash = ? AND check_id = ?
              AND disposition = 'pending' AND reservation_token = ?
            """,
            (sent_at.isoformat(), destination_hash, check_id, reservation_token),
        )
        if cursor.rowcount != 1:
            raise DispatchError("state_reservation_lost")
        connection.execute("COMMIT")
    except DispatchError:
        _rollback(connection)
        raise
    except sqlite3.Error as error:
        _rollback(connection)
        raise DispatchError("state_unavailable") from error


def _rollback(connection: sqlite3.Connection) -> None:
    with suppress(sqlite3.Error):
        connection.execute("ROLLBACK")


def _load_alerting_config(path: Path) -> tuple[bool, str, str]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DispatchError("invalid_operational_config") from error
    alerting = document.get("alerting")
    if not isinstance(alerting, Mapping) or set(alerting) != {
        "enabled",
        "channel",
        "destination_env",
    }:
        raise DispatchError("invalid_alerting_config")
    enabled = alerting.get("enabled")
    channel = alerting.get("channel")
    destination_env = alerting.get("destination_env")
    if (
        not isinstance(enabled, bool)
        or not isinstance(channel, str)
        or not isinstance(destination_env, str)
    ):
        raise DispatchError("invalid_alerting_config")
    _validate_environment_name(destination_env)
    return enabled, channel, destination_env


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--destination-env", required=True)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--operational-config", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--send", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        document = json.loads(arguments.report.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            raise DispatchError("invalid_health_report")
        typed_document = cast(Mapping[str, Any], document)
        send = arguments.send
        disabled = False
        if send:
            if arguments.operational_config is None:
                raise DispatchError("operational_config_required")
            enabled, channel, configured_destination = _load_alerting_config(
                arguments.operational_config
            )
            if enabled and (
                channel != CHANNEL or configured_destination != arguments.destination_env
            ):
                raise DispatchError("alerting_config_mismatch")
            disabled = not enabled
            send = enabled
        result = dispatch_health_report(
            typed_document,
            destination_env=arguments.destination_env,
            state_path=arguments.state_db,
            send=send,
            timeout_seconds=arguments.timeout_seconds,
        )
        if disabled:
            result = DispatchResult(result.report_status, "disabled", result.planned)
        print(json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")))
        return result.exit_code
    except (OSError, json.JSONDecodeError, DispatchError) as error:
        error_code = error.code if isinstance(error, DispatchError) else "input_unavailable"
        print(
            json.dumps(
                {
                    "schema_version": RESULT_SCHEMA,
                    "channel": CHANNEL,
                    "status": "error",
                    "error_code": error_code,
                    "sent": 0,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
