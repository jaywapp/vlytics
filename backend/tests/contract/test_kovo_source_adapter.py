"""Offline HTTP contracts for the production KOVO source adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

from vlytics.config import OperationalConfig
from vlytics.mirror.backfill import (
    BackfillRunner,
    BackfillScope,
    BulkCollectionBlocked,
    CollectionGate,
    ScopeLeaseUnavailable,
    SourceRequestFailed,
)
from vlytics.mirror.kovo import KOVO_BASE_URL, KovoSourceAdapter, KovoSyncJobHandler
from vlytics.mirror.models import SourceEndpoint
from vlytics.mirror.parser import KovoParser
from vlytics.ops.scheduler import RetryableJobError, TerminalJobError

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
SCOPE = BackfillScope("kovo", "001", "023", "201")


def _schedule_page(number: int, total_pages: int) -> bytes:
    return json.dumps(
        {
            "result": {"status": 200, "message": "synthetic"},
            "payload": {
                "content": [],
                "page": {
                    "number": number,
                    "size": 2,
                    "totalElements": total_pages * 2,
                    "totalPages": total_pages,
                },
            },
        },
        separators=(",", ":"),
    ).encode()


def test_production_adapter_pins_host_get_shape_and_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, content=_schedule_page(page, 2))

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    adapter = KovoSourceAdapter(SourceEndpoint.GAME_SCHEDULE, client=client, now=lambda: NOW)

    first = adapter.fetch_page(SCOPE, None, 2, requested_at=NOW, timeout_seconds=4.5)
    second = adapter.fetch_page(SCOPE, first.next_cursor, 2, requested_at=NOW)

    assert first.next_cursor == "1"
    assert second.next_cursor is None
    assert [request.method for request in requests] == ["GET", "GET"]
    assert all(request.url.host == "user-api.kovo.co.kr" for request in requests)
    assert requests[0].extensions["timeout"]["read"] == 4.5
    assert requests[0].url == httpx.URL(
        KOVO_BASE_URL + "/stat/game-schedule?gcode=001&seasonCode=023&leagueCode=201&page=0&size=2"
    )
    assert first.responses[0].redacted_url == KOVO_BASE_URL + "/stat/game-schedule"


def test_production_adapter_preserves_429_retry_after_without_parsing() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "7"}, content=b"busy")
        ),
        follow_redirects=False,
        trust_env=False,
    )
    adapter = KovoSourceAdapter(SourceEndpoint.GAME_SCHEDULE, client=client, now=lambda: NOW)

    page = adapter.fetch_page(SCOPE, None, 2, requested_at=NOW)

    assert page.next_cursor is None
    assert page.responses[0].status_code == 429
    assert page.responses[0].retry_after_seconds == 7
    assert page.responses[0].body_bytes == b"busy"


def test_production_adapter_is_blocked_until_op_001_is_approved() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    adapter = KovoSourceAdapter(SourceEndpoint.GAME_SCHEDULE, client=client)

    with pytest.raises(BulkCollectionBlocked, match="OP-001"):
        CollectionGate(False, "__REQUIRED_BY_OP_001__", 0, 0).authorize(adapter)


def test_production_adapter_rejects_redirects() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(307, headers={"Location": "https://example.invalid"})
        ),
        follow_redirects=False,
        trust_env=False,
    )
    adapter = KovoSourceAdapter(SourceEndpoint.GAME_SCHEDULE, client=client, now=lambda: NOW)

    with pytest.raises(ConnectionError, match="redirects"):
        adapter.fetch_page(SCOPE, None, 2, requested_at=NOW)


class _NoMatchResult:
    def scalar_one_or_none(self) -> None:
        return None


class _CapturingConnection:
    def __init__(self) -> None:
        self.statement = ""
        self.parameters: dict[str, object] = {}

    def __enter__(self) -> _CapturingConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, parameters: dict[str, object]) -> _NoMatchResult:
        self.statement = str(statement)
        self.parameters = parameters
        return _NoMatchResult()


class _CapturingEngine:
    def __init__(self) -> None:
        self.connection = _CapturingConnection()

    def connect(self) -> _CapturingConnection:
        return self.connection


def _operational_config() -> OperationalConfig:
    return OperationalConfig(
        {
            "results": {"provisional_stability_seconds": 1800},
            "source": {
                "bulk_collection_enabled": True,
                "permission_policy": "approved-synthetic-test-policy",
                "max_requests_per_minute": 60,
                "max_concurrency": 2,
                "scopes": [
                    {
                        "source": SCOPE.source,
                        "group_code": SCOPE.group_code,
                        "season_code": SCOPE.season_code,
                        "competition_code": SCOPE.competition_code,
                    }
                ],
            },
        },
        Path("synthetic.toml"),
        Path("synthetic.schema.json"),
    )


def test_handler_rejects_a_job_outside_approved_scopes_before_http() -> None:
    requests: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200)
        )
    )
    handler = KovoSyncJobHandler(
        cast(Engine, _CapturingEngine()),
        _operational_config(),
        KovoParser(),
        client=client,
    )

    with pytest.raises(TerminalJobError, match="invalid_source_job_contract"):
        handler.handle(
            {
                "job_type": "mirror.current_schedule",
                "payload": {
                    "source": "kovo",
                    "group_code": "001",
                    "season_code": "unapproved",
                    "competition_code": "201",
                },
            },
            now=NOW,
        )

    assert requests == []


def test_handler_resolves_match_id_only_inside_payload_scope() -> None:
    engine = _CapturingEngine()
    handler = KovoSyncJobHandler(
        cast(Engine, engine),
        _operational_config(),
        KovoParser(),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )

    with pytest.raises(TerminalJobError, match="invalid_source_job_contract"):
        handler.handle(
            {
                "job_type": "mirror.pre_cutoff_sync",
                "payload": {
                    "source": SCOPE.source,
                    "group_code": SCOPE.group_code,
                    "season_code": SCOPE.season_code,
                    "competition_code": SCOPE.competition_code,
                    "match_id": str(uuid4()),
                },
            },
            now=NOW,
        )

    assert "source_group_code=:group_code" in engine.connection.statement
    assert "source_season_code=:season_code" in engine.connection.statement
    assert "source_competition_code=:competition_code" in engine.connection.statement
    assert engine.connection.parameters["group_code"] == SCOPE.group_code


@pytest.mark.parametrize(
    ("source_error", "error_code"),
    (
        (ScopeLeaseUnavailable("busy"), "source_temporarily_unavailable"),
        (SourceRequestFailed("429 retries exhausted"), "source_request_failed"),
    ),
)
def test_handler_classifies_transient_source_failures_for_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    source_error: Exception,
    error_code: str,
) -> None:
    def fail_sync(
        _runner: BackfillRunner,
        _scope: BackfillScope,
        *,
        page_size: int = 2,
        deadline_at: datetime | None = None,
    ) -> int:
        del page_size, deadline_at
        raise source_error

    monkeypatch.setattr(BackfillRunner, "sync_once", fail_sync)
    handler = KovoSyncJobHandler(
        cast(Engine, _CapturingEngine()),
        _operational_config(),
        KovoParser(),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )

    with pytest.raises(RetryableJobError, match=error_code):
        handler.handle(
            {
                "job_type": "mirror.current_schedule",
                "payload": {
                    "source": SCOPE.source,
                    "group_code": SCOPE.group_code,
                    "season_code": SCOPE.season_code,
                    "competition_code": SCOPE.competition_code,
                },
            },
            now=NOW,
        )
