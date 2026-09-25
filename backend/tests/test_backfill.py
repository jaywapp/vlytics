from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from vlytics.mirror.backfill import (
    BackfillCheckpoint,
    BackfillRunner,
    BackfillScope,
    BulkCollectionBlocked,
    CheckpointConflict,
    CollectionGate,
    InMemoryCheckpointStore,
    InMemoryScopeLease,
    InvalidObservationTime,
    RequestRateLimiter,
    RetryBudgetExceeded,
    RetryPolicy,
    SourcePage,
    SyntheticSourceAdapter,
)
from vlytics.mirror.models import (
    IngestAction,
    SourceEndpoint,
    SourceRequestKey,
    SourceResponse,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
SCOPE = BackfillScope("kovo", "001", "023", "201")


def _response(code: str, *, status: int = 200, retry_after: int | None = None) -> SourceResponse:
    return SourceResponse(
        source="synthetic",
        request_key=SourceRequestKey(
            "001",
            SourceEndpoint.GAME_DETAIL,
            season_code="023",
            league_code="201",
            match_code=code,
        ),
        redacted_url="synthetic://game-detail",
        requested_at=NOW,
        received_at=NOW,
        status_code=status,
        body_bytes=b"{}",
        retry_after_seconds=retry_after,
    )


class _Sink:
    def __init__(self) -> None:
        self.codes: list[str] = []

    def ingest(self, response: SourceResponse) -> object:
        assert response.request_key.match_code is not None
        self.codes.append(response.request_key.match_code)
        return SimpleNamespace(action=IngestAction.APPEND_REVISION)


class _Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.seconds)

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


def _runner(
    adapter: SyntheticSourceAdapter,
    sink: _Sink,
    checkpoints: InMemoryCheckpointStore,
    clock: _Clock,
) -> BackfillRunner:
    return BackfillRunner(
        adapter=adapter,
        ingestion=sink,
        checkpoints=checkpoints,
        gate=CollectionGate(False, "__REQUIRED_BY_OP_001__", 0, 0),
        rate_limiter=RequestRateLimiter(60, monotonic=clock.monotonic, sleep=clock.sleep),
        scope_lease=InMemoryScopeLease(),
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_delay_seconds=1,
            backoff_multiplier=2,
            max_delay_seconds=30,
        ),
        now=clock.now,
        sleep=clock.sleep,
    )


def test_validation_then_batch_resumes_from_durable_cursor() -> None:
    pages = {
        None: SourcePage((_response("001"),), "page-2"),
        "page-2": SourcePage((_response("002"), _response("003")), None),
    }
    adapter = SyntheticSourceAdapter(pages)
    sink = _Sink()
    checkpoints = InMemoryCheckpointStore()
    clock = _Clock()

    first = _runner(adapter, sink, checkpoints, clock).run(SCOPE, max_pages=1)

    assert first.checkpoint.cursor == "page-2"
    assert first.checkpoint.phase == "batch"
    assert first.checkpoint.completed is False
    assert adapter.calls[0][1] == 2

    second = _runner(adapter, sink, checkpoints, clock).run(SCOPE)

    assert second.checkpoint.completed is True
    assert second.checkpoint.responses_ingested == 3
    assert sink.codes == ["001", "002", "003"]
    assert adapter.calls[1][0] == "page-2"
    assert adapter.calls[1][1] == 100


def test_retry_after_and_rate_limit_are_honored_before_checkpoint() -> None:
    adapter = SyntheticSourceAdapter(
        {
            None: (
                SourcePage((_response("001", status=429, retry_after=7),), None),
                SourcePage((_response("001"),), None),
            )
        }
    )
    clock = _Clock()
    result = _runner(adapter, _Sink(), InMemoryCheckpointStore(), clock).run(SCOPE)

    assert result.retries == 1
    assert result.requests == 2
    assert clock.seconds >= 7
    assert result.checkpoint.completed


def test_real_adapter_is_fail_closed_while_op_001_is_unresolved() -> None:
    adapter = SyntheticSourceAdapter({None: SourcePage((_response("001"),), None)})
    adapter.synthetic = False

    with pytest.raises(BulkCollectionBlocked, match="OP-001"):
        _runner(adapter, _Sink(), InMemoryCheckpointStore(), _Clock()).run(SCOPE)


def test_backdated_observation_is_rejected() -> None:
    class BackdatingAdapter(SyntheticSourceAdapter):
        def fetch_page(
            self,
            scope: BackfillScope,
            cursor: str | None,
            page_size: int,
            *,
            requested_at: datetime,
        ) -> SourcePage:
            del scope, cursor, page_size
            return SourcePage(
                (replace(_response("001"), received_at=requested_at - timedelta(seconds=1)),),
                None,
            )

    with pytest.raises(InvalidObservationTime, match="never be backdated"):
        _runner(BackdatingAdapter({}), _Sink(), InMemoryCheckpointStore(), _Clock()).run(SCOPE)


def test_fact_failure_does_not_advance_checkpoint() -> None:
    class FailingSink(_Sink):
        def ingest(self, response: SourceResponse) -> object:
            super().ingest(response)
            raise RuntimeError("fact transaction rolled back")

    checkpoints = InMemoryCheckpointStore()
    adapter = SyntheticSourceAdapter({None: SourcePage((_response("001"),), "page-2")})

    with pytest.raises(RuntimeError, match="fact transaction"):
        _runner(adapter, FailingSink(), checkpoints, _Clock()).run(SCOPE)

    assert checkpoints.load(SCOPE) is None


def test_checkpoint_generation_rejects_stale_and_phase_regression() -> None:
    store = InMemoryCheckpointStore()
    initial = store.advance(
        BackfillCheckpoint(SCOPE, "page-2", "batch", False, 1, 1, NOW, generation=0),
        expected_generation=0,
    )

    with pytest.raises(CheckpointConflict, match="generation"):
        store.advance(
            replace(initial, cursor="stale", pages_completed=2),
            expected_generation=0,
        )

    with pytest.raises(CheckpointConflict, match="phase"):
        store.advance(
            replace(initial, phase="validation", pages_completed=2),
            expected_generation=initial.generation,
        )


def test_retry_after_above_policy_stops_without_sleeping() -> None:
    adapter = SyntheticSourceAdapter(
        {None: SourcePage((_response("001", status=429, retry_after=31),), None)}
    )
    clock = _Clock()

    with pytest.raises(RetryBudgetExceeded, match="Retry-After"):
        _runner(adapter, _Sink(), InMemoryCheckpointStore(), clock).run(SCOPE)

    assert clock.seconds == 0


def test_timeout_uses_bounded_deterministic_jitter() -> None:
    adapter = SyntheticSourceAdapter(
        {
            None: (
                TimeoutError("synthetic timeout"),
                SourcePage((_response("001"),), None),
            )
        }
    )
    sink = _Sink()
    checkpoints = InMemoryCheckpointStore()
    clock = _Clock()
    runner = BackfillRunner(
        adapter=adapter,
        ingestion=sink,
        checkpoints=checkpoints,
        gate=CollectionGate(False, "__REQUIRED_BY_OP_001__", 0, 0),
        rate_limiter=RequestRateLimiter(600, monotonic=clock.monotonic, sleep=clock.sleep),
        scope_lease=InMemoryScopeLease(),
        retry_policy=RetryPolicy(max_attempts=2, max_delay_seconds=10),
        now=clock.now,
        sleep=clock.sleep,
        jitter=lambda attempt: 0.25 * attempt,
    )

    result = runner.run(SCOPE)

    assert result.retries == 1
    assert clock.seconds >= 1.25
    assert result.checkpoint.completed


def test_live_limits_cannot_exceed_approval_or_use_process_only_lease() -> None:
    adapter = SyntheticSourceAdapter({None: SourcePage((_response("001"),), None)})
    adapter.synthetic = False
    gate = CollectionGate(True, "approved-test-policy", 10, 2)

    with pytest.raises(BulkCollectionBlocked, match="rate exceeds"):
        RequestRateLimiter.from_gate(
            gate,
            adapter,
            requested_rate=11,
        )

    with pytest.raises(BulkCollectionBlocked, match="PostgreSQL scope lease"):
        BackfillRunner(
            adapter=adapter,
            ingestion=_Sink(),
            checkpoints=InMemoryCheckpointStore(),
            gate=gate,
            rate_limiter=RequestRateLimiter.from_gate(
                gate,
                adapter,
                requested_rate=10,
            ),
            scope_lease=InMemoryScopeLease(),
            requested_concurrency=2,
        )
