"""Fail-closed HTTP adapter and durable sync handler for the reviewed KOVO source."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from sqlalchemy import Engine, text

from vlytics.config import OperationalConfig
from vlytics.mirror.backfill import (
    BackfillRunner,
    BackfillScope,
    CollectionGate,
    PostgresCheckpointStore,
    PostgresScopeLease,
    RequestRateLimiter,
    RetryPolicy,
    SourcePage,
)
from vlytics.mirror.ingestion import MirrorIngestionService
from vlytics.mirror.models import SourceEndpoint, SourceRequestKey, SourceResponse
from vlytics.mirror.parser import KovoParser
from vlytics.mirror.repositories.facts import MirrorFactRepository
from vlytics.mirror.repositories.raw_snapshots import RawSnapshotRepository
from vlytics.ops.sync import scope_from_payload

KOVO_BASE_URL = "https://user-api.kovo.co.kr"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def scopes_from_operational_config(
    operational_config: OperationalConfig,
) -> tuple[BackfillScope, ...]:
    """Return exact approved KOVO scopes from the validated operational config."""

    source = operational_config.values.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("operational source configuration must be an object")
    rows = source.get("scopes")
    if not isinstance(rows, list):
        raise ValueError("operational source scopes must be an array")
    scopes: list[BackfillScope] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("operational source scope must be an object")
        values = tuple(
            row.get(key) for key in ("source", "group_code", "season_code", "competition_code")
        )
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError("operational source scope contains an invalid identifier")
        scope = BackfillScope(*values)  # type: ignore[arg-type]
        if scope.source != "kovo":
            raise ValueError("only KOVO source scopes are supported")
        scopes.append(scope)
    if len({scope.key for scope in scopes}) != len(scopes):
        raise ValueError("operational source scopes must be unique")
    return tuple(scopes)


class KovoSourceAdapter:
    """GET-only adapter pinned to the reviewed KOVO host and request shapes."""

    synthetic = False

    def __init__(
        self,
        endpoint: SourceEndpoint,
        *,
        match_code: str | None = None,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        if endpoint is SourceEndpoint.GAME_DETAIL and not (match_code or "").strip():
            raise ValueError("game detail collection requires a match code")
        if endpoint is not SourceEndpoint.GAME_DETAIL and match_code is not None:
            raise ValueError("match code is valid only for game detail collection")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self.endpoint = endpoint
        self.match_code = match_code
        self._client = client or httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
        )
        self._now = now
        self._max_response_bytes = max_response_bytes

    def fetch_page(
        self,
        scope: Any,
        cursor: str | None,
        page_size: int,
        *,
        requested_at: datetime,
    ) -> SourcePage:
        if scope.source != "kovo":
            raise ValueError("KOVO adapter requires the kovo source scope")
        if page_size < 1:
            raise ValueError("page_size must be positive")
        page_number = self._page_number(cursor)
        path, params = self._request(scope, page_number, page_size)
        try:
            response = self._client.get(
                KOVO_BASE_URL + path,
                params=params,
                headers={"Accept": "application/json"},
            )
        except httpx.TimeoutException as error:
            raise TimeoutError("KOVO request timed out") from error
        except httpx.RequestError as error:
            raise ConnectionError("KOVO request failed") from error
        if response.is_redirect:
            raise ConnectionError("KOVO redirects are not accepted")
        body = response.content
        if len(body) > self._max_response_bytes:
            raise ConnectionError("KOVO response exceeded the configured size limit")
        received_at = self._now()
        if received_at < requested_at:
            received_at = requested_at
        observed = SourceResponse(
            source="kovo",
            request_key=SourceRequestKey(
                scope.group_code,
                self.endpoint,
                season_code=scope.season_code,
                league_code=scope.competition_code,
                match_code=self.match_code,
            ),
            redacted_url=KOVO_BASE_URL + path,
            requested_at=requested_at,
            received_at=received_at,
            status_code=response.status_code,
            body_bytes=body,
            retry_after_seconds=_retry_after_seconds(
                response.headers.get("Retry-After"), received_at
            ),
        )
        return SourcePage((observed,), self._next_cursor(body, page_number, response.status_code))

    def _request(
        self,
        scope: Any,
        page_number: int,
        page_size: int,
    ) -> tuple[str, dict[str, str | int]]:
        common: dict[str, str | int] = {"gcode": scope.group_code}
        if self.endpoint is SourceEndpoint.SEASON_LIST:
            return "/stat/season-list", common
        common.update(
            {
                "seasonCode": scope.season_code,
                "leagueCode": scope.competition_code,
            }
        )
        if self.endpoint is SourceEndpoint.GAME_DETAIL:
            assert self.match_code is not None
            return f"/stat/game-schedule/{quote(self.match_code, safe='')}", common
        common.update({"page": page_number, "size": page_size})
        return "/stat/game-schedule", common

    def _page_number(self, cursor: str | None) -> int:
        if self.endpoint is not SourceEndpoint.GAME_SCHEDULE:
            if cursor is not None:
                raise ValueError("non-paginated KOVO endpoint received a cursor")
            return 0
        if cursor is None:
            return 0
        if not cursor.isascii() or not cursor.isdecimal():
            raise ValueError("KOVO page cursor must be a non-negative decimal integer")
        return int(cursor)

    def _next_cursor(self, body: bytes, current: int, status_code: int) -> str | None:
        if self.endpoint is not SourceEndpoint.GAME_SCHEDULE or status_code != 200:
            return None
        try:
            document = json.loads(body)
            page = document["payload"]["page"]
            number = page["number"]
            total_pages = page["totalPages"]
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            return None
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or isinstance(total_pages, bool)
            or not isinstance(total_pages, int)
            or number != current
            or total_pages < 0
        ):
            return None
        return str(number + 1) if number + 1 < total_pages else None


def _retry_after_seconds(value: str | None, received_at: datetime) -> int | None:
    if value is None:
        return None
    stripped = value.strip()
    if stripped.isascii() and stripped.isdecimal():
        return int(stripped)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0, math.ceil((retry_at - received_at).total_seconds()))


class KovoSyncJobHandler:
    """Build the reviewed live adapter per durable job and consume all pages."""

    def __init__(
        self,
        collector_engine: Engine,
        operational_config: OperationalConfig,
        parser: KovoParser,
        *,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._collector_engine = collector_engine
        self._gate = CollectionGate.from_operational_config(operational_config)
        self._approved_scopes = {
            scope.key for scope in scopes_from_operational_config(operational_config)
        }
        if not self._approved_scopes:
            raise ValueError("KOVO handler requires at least one approved source scope")
        results = operational_config.values.get("results")
        if not isinstance(results, Mapping):
            raise ValueError("operational results configuration must be an object")
        stability_seconds = results.get("provisional_stability_seconds")
        if (
            isinstance(stability_seconds, bool)
            or not isinstance(stability_seconds, int)
            or stability_seconds < 0
        ):
            raise ValueError("provisional stability must be a non-negative integer")
        self._result_stability = timedelta(seconds=stability_seconds)
        self._parser = parser
        self._client = client or httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
        )
        self._now = now
        self._retry_policy = retry_policy or RetryPolicy()
        self._rate_limiter = RequestRateLimiter.from_gate(
            self._gate,
            KovoSourceAdapter(SourceEndpoint.GAME_SCHEDULE, client=self._client),
            requested_rate=None,
        )

    def handle(self, job: Mapping[str, Any], *, now: datetime) -> None:
        del now
        payload = job.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("sync job payload must be an object")
        scope = scope_from_payload(payload)
        if scope.key not in self._approved_scopes:
            raise ValueError("sync job scope is not approved by operational config")
        endpoint, match_code = self._target(job, payload, scope)
        adapter = KovoSourceAdapter(
            endpoint,
            match_code=match_code,
            client=self._client,
            now=self._now,
        )
        deadline = job.get("deadline_at")
        if deadline is not None and not isinstance(deadline, datetime):
            raise ValueError("sync job deadline must be a datetime")
        with self._collector_engine.connect() as connection:
            runner = BackfillRunner(
                adapter=adapter,
                ingestion=MirrorIngestionService(
                    RawSnapshotRepository(self._collector_engine),
                    MirrorFactRepository(
                        self._collector_engine,
                        result_stability=self._result_stability,
                    ),
                    self._parser,
                ),
                checkpoints=PostgresCheckpointStore(connection),
                gate=self._gate,
                rate_limiter=self._rate_limiter,
                scope_lease=PostgresScopeLease(connection, lease_key="source:kovo:global"),
                requested_concurrency=1,
                wait_after_global_lease=True,
                retry_policy=self._retry_policy,
                now=self._now,
            )
            runner.sync_once(scope, deadline_at=deadline)

    def _target(
        self,
        job: Mapping[str, Any],
        payload: Mapping[str, object],
        scope: BackfillScope,
    ) -> tuple[SourceEndpoint, str | None]:
        job_type = str(job.get("job_type"))
        if job_type == "mirror.current_schedule":
            return SourceEndpoint.GAME_SCHEDULE, None
        if job_type not in {
            "mirror.pre_cutoff_sync",
            "mirror.final_result",
            "mirror.correction_recheck",
        }:
            raise ValueError("unsupported KOVO sync job type")
        return SourceEndpoint.GAME_DETAIL, self._match_code(payload, scope)

    def _match_code(
        self,
        payload: Mapping[str, object],
        scope: BackfillScope,
    ) -> str:
        parameters: dict[str, object] = {
            "source": scope.source,
            "group_code": scope.group_code,
            "season_code": scope.season_code,
            "competition_code": scope.competition_code,
        }
        match_code = payload.get("match_code")
        if isinstance(match_code, str) and match_code.strip():
            statement = """
                SELECT source_match_code
                FROM mirror.matches
                WHERE source=:source AND source_group_code=:group_code
                  AND source_season_code=:season_code
                  AND source_competition_code=:competition_code
                  AND source_match_code=:match_code
            """
            parameters["match_code"] = match_code
        else:
            match_id = payload.get("match_id")
            try:
                parameters["match_id"] = UUID(str(match_id))
            except (TypeError, ValueError) as error:
                raise ValueError("KOVO detail sync job has no source match code") from error
            statement = """
                SELECT source_match_code
                FROM mirror.matches
                WHERE id=:match_id AND source=:source
                  AND source_group_code=:group_code
                  AND source_season_code=:season_code
                  AND source_competition_code=:competition_code
            """
        with self._collector_engine.connect() as connection:
            resolved = connection.execute(text(statement), parameters).scalar_one_or_none()
        if resolved is None:
            raise ValueError("KOVO detail sync job match is outside its approved scope")
        return str(resolved)


def build_kovo_sync_handlers(
    collector_engine: Engine,
    operational_config: OperationalConfig,
    parser: KovoParser,
    *,
    client: httpx.Client | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, KovoSyncJobHandler]:
    """Return the handler map that replaces disabled source handlers when approved."""

    handler = KovoSyncJobHandler(
        collector_engine,
        operational_config,
        parser,
        client=client,
        now=now,
    )
    return {
        "mirror.pre_cutoff_sync": handler,
        "mirror.current_schedule": handler,
        "mirror.final_result": handler,
        "mirror.correction_recheck": handler,
    }
