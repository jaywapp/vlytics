from datetime import UTC, datetime

import pytest

from vlytics.config import OperationalConfigError, Settings
from vlytics.worker import Worker, main


@pytest.mark.asyncio
async def test_worker_without_dispatcher_fails_closed() -> None:
    worker = Worker(Settings())

    with pytest.raises(OperationalConfigError, match="dispatcher"):
        await worker.run_once()


def test_worker_once_entrypoint_rejects_missing_database_runtime_config() -> None:
    assert main(["--once"]) == 2


@pytest.mark.asyncio
async def test_worker_executes_injected_durable_dispatcher() -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)

    class Dispatcher:
        def __init__(self) -> None:
            self.seen: list[datetime] = []

        def run_once(self, *, now: datetime) -> int:
            self.seen.append(now)
            return 1

    dispatcher = Dispatcher()
    worker = Worker(Settings(), dispatcher=dispatcher, now=lambda: now)

    assert await worker.run_once() == 1
    assert dispatcher.seen == [now]
