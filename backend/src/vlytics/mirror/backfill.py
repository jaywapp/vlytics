"""Resumable, rate-limited source backfill orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import Connection, text
from sqlalchemy.engine import RowMapping

from vlytics.mirror.models import IngestAction, SourceResponse

if TYPE_CHECKING:
    from vlytics.config import OperationalConfig


class BulkCollectionBlocked(RuntimeError):
    """Live collection is not fully authorized by OP-001."""


class SourceRequestFailed(RuntimeError):
    """A source page could not be obtained inside the retry policy."""


class RetryBudgetExceeded(SourceRequestFailed):
    """A server delay would cross the approved policy or job deadline."""


class InvalidObservationTime(ValueError):
    """An adapter tried to backdate an observation."""


class CheckpointConflict(RuntimeError):
    """Another worker advanced the same scope generation first."""


class ScopeLeaseUnavailable(RuntimeError):
    """Every approved per-scope concurrency slot is leased."""


@dataclass(frozen=True)
class BackfillScope:
    source: str
    group_code: str
    season_code: str
    competition_code: str

    @property
    def key(self) -> str:
        return ":".join((self.source, self.group_code, self.season_code, self.competition_code))


@dataclass(frozen=True)
class BackfillCheckpoint:
    scope: BackfillScope
    cursor: str | None
    phase: str
    completed: bool
    pages_completed: int
    responses_ingested: int
    updated_at: datetime
    generation: int = 0


@dataclass(frozen=True)
class SourcePage:
    responses: tuple[SourceResponse, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class BackfillResult:
    checkpoint: BackfillCheckpoint
    requests: int
    retries: int


class SourceAdapter(Protocol):
    synthetic: bool

    def fetch_page(
        self,
        scope: BackfillScope,
        cursor: str | None,
        page_size: int,
        *,
        requested_at: datetime,
    ) -> SourcePage: ...


class DurableIngestionSink(Protocol):
    def ingest(self, response: SourceResponse) -> object:
        """Return only after the receipt and accepted facts are committed."""


class CheckpointStore(Protocol):
    def load(self, scope: BackfillScope) -> BackfillCheckpoint | None: ...

    def advance(
        self, checkpoint: BackfillCheckpoint, *, expected_generation: int
    ) -> BackfillCheckpoint: ...


class ScopeLease(Protocol):
    def acquire(self, scope: BackfillScope, max_concurrency: int) -> int | None: ...

    def release(self, scope: BackfillScope, slot: int) -> None: ...


_PHASE_RANK = {"validation": 0, "batch": 1, "completed": 2}


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self._items: dict[str, BackfillCheckpoint] = {}

    def load(self, scope: BackfillScope) -> BackfillCheckpoint | None:
        return self._items.get(scope.key)

    def advance(
        self, checkpoint: BackfillCheckpoint, *, expected_generation: int
    ) -> BackfillCheckpoint:
        current = self._items.get(checkpoint.scope.key)
        actual = 0 if current is None else current.generation
        if actual != expected_generation:
            raise CheckpointConflict("checkpoint generation changed")
        if current is not None:
            if current.completed:
                raise CheckpointConflict("completed checkpoint cannot reopen")
            if _PHASE_RANK[checkpoint.phase] < _PHASE_RANK[current.phase]:
                raise CheckpointConflict("checkpoint phase cannot regress")
            if checkpoint.pages_completed <= current.pages_completed:
                raise CheckpointConflict("checkpoint page count must advance")
            if checkpoint.responses_ingested < current.responses_ingested:
                raise CheckpointConflict("checkpoint response count cannot regress")
            if checkpoint.updated_at < current.updated_at:
                raise CheckpointConflict("checkpoint time cannot regress")
        advanced = replace(checkpoint, generation=expected_generation + 1)
        self._items[checkpoint.scope.key] = advanced
        return advanced


class PostgresCheckpointStore:
    """Generation-CAS checkpoint repository on a collector connection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def load(self, scope: BackfillScope) -> BackfillCheckpoint | None:
        row = (
            self._connection.execute(
                text(
                    """
                    SELECT cursor, phase, completed, pages_completed,
                           responses_ingested, updated_at, generation
                    FROM ops.sync_checkpoints
                    WHERE source=:source AND source_group_code=:group_code
                      AND source_season_code=:season_code
                      AND source_competition_code=:competition_code
                    """
                ),
                _scope_params(scope),
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _checkpoint_from_row(scope, row)

    def advance(
        self, checkpoint: BackfillCheckpoint, *, expected_generation: int
    ) -> BackfillCheckpoint:
        params: dict[str, object] = {
            **_scope_params(checkpoint.scope),
            "cursor": checkpoint.cursor,
            "phase": checkpoint.phase,
            "completed": checkpoint.completed,
            "pages_completed": checkpoint.pages_completed,
            "responses_ingested": checkpoint.responses_ingested,
            "updated_at": checkpoint.updated_at,
            "expected": expected_generation,
            "next": expected_generation + 1,
        }
        if expected_generation == 0:
            statement = """
                INSERT INTO ops.sync_checkpoints (
                    source, source_group_code, source_season_code,
                    source_competition_code, cursor, phase, completed,
                    pages_completed, responses_ingested, updated_at, generation
                ) VALUES (
                    :source, :group_code, :season_code, :competition_code,
                    :cursor, :phase, :completed, :pages_completed,
                    :responses_ingested, :updated_at, :next
                )
                ON CONFLICT (
                    source, source_group_code, source_season_code,
                    source_competition_code
                ) DO NOTHING
                RETURNING cursor, phase, completed, pages_completed,
                          responses_ingested, updated_at, generation
            """
        else:
            statement = """
                UPDATE ops.sync_checkpoints
                SET cursor=:cursor, phase=:phase, completed=:completed,
                    pages_completed=:pages_completed,
                    responses_ingested=:responses_ingested,
                    updated_at=:updated_at, generation=:next
                WHERE source=:source AND source_group_code=:group_code
                  AND source_season_code=:season_code
                  AND source_competition_code=:competition_code
                  AND generation=:expected
                RETURNING cursor, phase, completed, pages_completed,
                          responses_ingested, updated_at, generation
            """
        row = self._connection.execute(text(statement), params).mappings().one_or_none()
        if row is None:
            raise CheckpointConflict("checkpoint generation changed")
        return _checkpoint_from_row(checkpoint.scope, row)


def _checkpoint_from_row(scope: BackfillScope, row: RowMapping) -> BackfillCheckpoint:
    updated_at = row["updated_at"]
    if not isinstance(updated_at, datetime):
        raise TypeError("checkpoint updated_at must be a datetime")
    return BackfillCheckpoint(
        scope=scope,
        cursor=None if row["cursor"] is None else str(row["cursor"]),
        phase=str(row["phase"]),
        completed=bool(row["completed"]),
        pages_completed=int(str(row["pages_completed"])),
        responses_ingested=int(str(row["responses_ingested"])),
        updated_at=updated_at,
        generation=int(str(row["generation"])),
    )


def _scope_params(scope: BackfillScope) -> dict[str, str]:
    return {
        "source": scope.source,
        "group_code": scope.group_code,
        "season_code": scope.season_code,
        "competition_code": scope.competition_code,
    }


@dataclass(frozen=True)
class CollectionGate:
    bulk_collection_enabled: bool
    permission_policy: str
    max_requests_per_minute: int
    max_concurrency: int

    @classmethod
    def from_operational_config(cls, config: OperationalConfig) -> CollectionGate:
        source = config.values.get("source")
        if not isinstance(source, dict):
            raise ValueError("operational config source section is missing")
        values = (
            source.get("bulk_collection_enabled"),
            source.get("permission_policy"),
            source.get("max_requests_per_minute"),
            source.get("max_concurrency"),
        )
        if (
            not isinstance(values[0], bool)
            or not isinstance(values[1], str)
            or isinstance(values[2], bool)
            or not isinstance(values[2], int)
            or isinstance(values[3], bool)
            or not isinstance(values[3], int)
        ):
            raise ValueError("operational config source section has invalid values")
        return cls(values[0], values[1], values[2], values[3])

    def authorize(self, adapter: SourceAdapter) -> None:
        if adapter.synthetic:
            return
        if (
            not self.bulk_collection_enabled
            or not self.permission_policy.strip()
            or self.permission_policy.startswith("__REQUIRED")
            or self.max_requests_per_minute <= 0
            or self.max_concurrency <= 0
        ):
            raise BulkCollectionBlocked(
                "OP-001 is unresolved: real bulk collection remains disabled"
            )

    def approved_rate(self, adapter: SourceAdapter, requested: int | None) -> int:
        self.authorize(adapter)
        if adapter.synthetic:
            if requested is None or requested <= 0:
                raise ValueError("synthetic collection requires a positive test rate")
            return requested
        value = self.max_requests_per_minute if requested is None else requested
        if value <= 0 or value > self.max_requests_per_minute:
            raise BulkCollectionBlocked("requested rate exceeds the OP-001 approval")
        return value

    def approved_concurrency(self, adapter: SourceAdapter, requested: int | None) -> int:
        self.authorize(adapter)
        if adapter.synthetic:
            value = 1 if requested is None else requested
            if value <= 0:
                raise ValueError("synthetic concurrency must be positive")
            return value
        value = self.max_concurrency if requested is None else requested
        if value <= 0 or value > self.max_concurrency:
            raise BulkCollectionBlocked("requested concurrency exceeds the OP-001 approval")
        return value


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 30.0

    def delay(self, attempt: int, retry_after_seconds: int | None, jitter_seconds: float) -> float:
        if retry_after_seconds is not None:
            delay = float(retry_after_seconds)
            if delay > self.max_delay_seconds:
                raise RetryBudgetExceeded("Retry-After exceeds the approved retry delay policy")
            return delay
        base = self.initial_delay_seconds * self.backoff_multiplier ** (attempt - 1)
        return min(base + max(jitter_seconds, 0.0), self.max_delay_seconds)


class RequestRateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.requests_per_minute = requests_per_minute
        self._interval = 60.0 / requests_per_minute
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request: float | None = None

    @classmethod
    def from_gate(
        cls,
        gate: CollectionGate,
        adapter: SourceAdapter,
        *,
        requested_rate: int | None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> RequestRateLimiter:
        return cls(
            gate.approved_rate(adapter, requested_rate),
            monotonic=monotonic,
            sleep=sleep,
        )

    def wait(self) -> None:
        now = self._monotonic()
        if self._last_request is not None:
            remaining = self._interval - (now - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request = now


class InMemoryScopeLease:
    def __init__(self) -> None:
        self._slots: set[tuple[str, int]] = set()

    def acquire(self, scope: BackfillScope, max_concurrency: int) -> int | None:
        for slot in range(max_concurrency):
            key = (scope.key, slot)
            if key not in self._slots:
                self._slots.add(key)
                return slot
        return None

    def release(self, scope: BackfillScope, slot: int) -> None:
        self._slots.discard((scope.key, slot))


class PostgresScopeLease:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def acquire(self, scope: BackfillScope, max_concurrency: int) -> int | None:
        for slot in range(max_concurrency):
            acquired = self._connection.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:scope, :slot))"),
                {"scope": scope.key, "slot": slot},
            ).scalar_one()
            if bool(acquired):
                return slot
        return None

    def release(self, scope: BackfillScope, slot: int) -> None:
        self._connection.execute(
            text("SELECT pg_advisory_unlock(hashtextextended(:scope, :slot))"),
            {"scope": scope.key, "slot": slot},
        )


class BackfillRunner:
    def __init__(
        self,
        *,
        adapter: SourceAdapter,
        ingestion: DurableIngestionSink,
        checkpoints: CheckpointStore,
        gate: CollectionGate,
        rate_limiter: RequestRateLimiter,
        scope_lease: ScopeLease,
        requested_concurrency: int | None = None,
        retry_policy: RetryPolicy | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[int], float] = lambda _attempt: 0.0,
        retry_exceptions: tuple[type[Exception], ...] = (
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    ) -> None:
        self._adapter = adapter
        self._ingestion = ingestion
        self._checkpoints = checkpoints
        self._gate = gate
        self._rate_limiter = rate_limiter
        self._scope_lease = scope_lease
        self._max_concurrency = gate.approved_concurrency(adapter, requested_concurrency)
        if not adapter.synthetic and not isinstance(scope_lease, PostgresScopeLease):
            raise BulkCollectionBlocked("live collection requires a PostgreSQL scope lease")
        if (
            not adapter.synthetic
            and rate_limiter.requests_per_minute > gate.max_requests_per_minute
        ):
            raise BulkCollectionBlocked("rate limiter exceeds the OP-001 approval")
        self._retry_policy = retry_policy or RetryPolicy()
        self._now = now
        self._sleep = sleep
        self._jitter = jitter
        self._retry_exceptions = retry_exceptions

    def run(
        self,
        scope: BackfillScope,
        *,
        validation_page_size: int = 2,
        batch_page_size: int = 100,
        max_pages: int | None = None,
        deadline_at: datetime | None = None,
    ) -> BackfillResult:
        if validation_page_size <= 0 or batch_page_size <= 0:
            raise ValueError("page sizes must be positive")
        self._gate.authorize(self._adapter)
        slot = self._scope_lease.acquire(scope, self._max_concurrency)
        if slot is None:
            raise ScopeLeaseUnavailable("approved scope concurrency is exhausted")
        try:
            return self._run_leased(
                scope,
                validation_page_size,
                batch_page_size,
                max_pages,
                deadline_at,
            )
        finally:
            self._scope_lease.release(scope, slot)

    def sync_once(
        self,
        scope: BackfillScope,
        *,
        page_size: int = 2,
        deadline_at: datetime | None = None,
    ) -> int:
        """Fetch one recurring sync page; the durable ops job is its checkpoint."""

        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self._gate.authorize(self._adapter)
        slot = self._scope_lease.acquire(scope, self._max_concurrency)
        if slot is None:
            raise ScopeLeaseUnavailable("approved scope concurrency is exhausted")
        try:
            page, _, _ = self._fetch_with_retry(scope, None, page_size, deadline_at)
            accepted = 0
            for response in page.responses:
                result = self._ingestion.ingest(response)
                if getattr(result, "action", None) in {
                    IngestAction.RETRY_LATER,
                    IngestAction.QUARANTINE,
                }:
                    raise SourceRequestFailed(
                        f"ingestion did not accept {response.request_key.fingerprint()}"
                    )
                accepted += 1
            return accepted
        finally:
            self._scope_lease.release(scope, slot)

    def _run_leased(
        self,
        scope: BackfillScope,
        validation_page_size: int,
        batch_page_size: int,
        max_pages: int | None,
        deadline_at: datetime | None,
    ) -> BackfillResult:
        checkpoint = self._checkpoints.load(scope) or BackfillCheckpoint(
            scope, None, "validation", False, 0, 0, self._now(), 0
        )
        if checkpoint.completed:
            return BackfillResult(checkpoint, 0, 0)
        requests = retries = pages_this_run = 0
        while max_pages is None or pages_this_run < max_pages:
            size = validation_page_size if checkpoint.phase == "validation" else batch_page_size
            page, page_requests, page_retries = self._fetch_with_retry(
                scope, checkpoint.cursor, size, deadline_at
            )
            requests += page_requests
            retries += page_retries
            accepted = 0
            for response in page.responses:
                result = self._ingestion.ingest(response)
                if getattr(result, "action", None) in {
                    IngestAction.RETRY_LATER,
                    IngestAction.QUARANTINE,
                }:
                    raise SourceRequestFailed(
                        f"ingestion did not accept {response.request_key.fingerprint()}"
                    )
                accepted += 1
            phase = "batch" if checkpoint.phase == "validation" else checkpoint.phase
            completed = page.next_cursor is None
            desired = replace(
                checkpoint,
                cursor=page.next_cursor,
                phase="completed" if completed else phase,
                completed=completed,
                pages_completed=checkpoint.pages_completed + 1,
                responses_ingested=checkpoint.responses_ingested + accepted,
                updated_at=self._now(),
            )
            checkpoint = self._checkpoints.advance(
                desired, expected_generation=checkpoint.generation
            )
            pages_this_run += 1
            if completed:
                break
        return BackfillResult(checkpoint, requests, retries)

    def _fetch_with_retry(
        self,
        scope: BackfillScope,
        cursor: str | None,
        page_size: int,
        deadline_at: datetime | None,
    ) -> tuple[SourcePage, int, int]:
        last_error: Exception | None = None
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            self._rate_limiter.wait()
            requested_at = self._now()
            try:
                page = self._adapter.fetch_page(scope, cursor, page_size, requested_at=requested_at)
            except self._retry_exceptions as error:
                last_error = error
                if attempt == self._retry_policy.max_attempts:
                    break
                self._sleep_for_retry(attempt, None, deadline_at)
                continue
            retry_after: int | None = None
            retryable = False
            for response in page.responses:
                if response.received_at < requested_at:
                    raise InvalidObservationTime(
                        "received_at predates this request; observed_at must never be backdated"
                    )
                status = response.status_code
                if status == 429 or (status is not None and status >= 500):
                    self._ingestion.ingest(response)
                    retryable = True
                    if response.retry_after_seconds is not None:
                        retry_after = max(retry_after or 0, response.retry_after_seconds)
            if not retryable:
                return page, attempt, attempt - 1
            if attempt == self._retry_policy.max_attempts:
                break
            self._sleep_for_retry(attempt, retry_after, deadline_at)
        message = f"source page failed after {self._retry_policy.max_attempts} attempts"
        if last_error is not None:
            raise SourceRequestFailed(message) from last_error
        raise SourceRequestFailed(message)

    def _sleep_for_retry(
        self,
        attempt: int,
        retry_after_seconds: int | None,
        deadline_at: datetime | None,
    ) -> None:
        delay = self._retry_policy.delay(attempt, retry_after_seconds, self._jitter(attempt))
        if deadline_at is not None and self._now().timestamp() + delay >= deadline_at.timestamp():
            raise RetryBudgetExceeded("retry delay would reach or cross the job deadline")
        self._sleep(delay)


class SyntheticSourceAdapter:
    synthetic = True

    def __init__(
        self,
        pages: Mapping[
            str | None,
            SourcePage | Exception | Sequence[SourcePage | Exception],
        ],
    ) -> None:
        self._pages = {
            cursor: [value] if isinstance(value, SourcePage | Exception) else list(value)
            for cursor, value in pages.items()
        }
        self.calls: list[tuple[str | None, int, datetime]] = []

    def fetch_page(
        self,
        scope: BackfillScope,
        cursor: str | None,
        page_size: int,
        *,
        requested_at: datetime,
    ) -> SourcePage:
        del scope
        self.calls.append((cursor, page_size, requested_at))
        scripted = self._pages.get(cursor)
        if not scripted:
            raise SourceRequestFailed(f"no synthetic page scripted for cursor {cursor!r}")
        item = scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return SourcePage(
            tuple(
                replace(
                    response,
                    requested_at=requested_at,
                    received_at=max(requested_at, response.received_at),
                )
                for response in item.responses
            ),
            item.next_cursor,
        )
