from datetime import UTC, datetime
from decimal import Decimal

import pytest

from vlytics.config import OperationalConfigError, Settings
from vlytics.engine.providers import (
    LiveProviderPlan,
    ProviderName,
    ProviderVariant,
    RuntimeProviderPlan,
    VersionPolicy,
)
from vlytics.engine.providers.base import PROMPT_TEMPLATE_HASH
from vlytics.worker import Worker, _durable_provider_budget_limits, main


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


def test_worker_builds_krw_budget_limits_for_only_the_active_provider() -> None:
    variant = ProviderVariant(
        variant_id="openai-home-v1",
        provider=ProviderName.OPENAI,
        requested_model_id="openai-pinned-v1",
        pinned_model_version="openai-pinned-v1",
        version_policy=VersionPolicy.IMMUTABLE_MODEL_ID,
        prompt_version="independent-volleyball-v1",
        prompt_hash=PROMPT_TEMPLATE_HASH,
        distribution_version="ai-direct-set-v1",
        max_input_tokens=4_000,
        max_output_tokens=1_000,
        input_cost_per_million=Decimal("2600"),
        output_cost_per_million=Decimal("13000"),
        enabled=True,
        op003_resolved=True,
        pricing_currency="KRW",
    )
    plan = LiveProviderPlan(
        budget_currency="KRW",
        providers=(
            RuntimeProviderPlan(
                variant=variant,
                api_key_env="VLYTICS_OPENAI_API_KEY",
                daily_budget=Decimal("1000"),
                monthly_budget=Decimal("10000"),
                max_calls_per_match=1,
                daily_call_limit=10,
                monthly_call_limit=100,
            ),
        ),
        pricing_currency="USD",
        pricing_to_budget_rate=Decimal("1300"),
    )

    limits = _durable_provider_budget_limits(plan)

    assert set(limits) == {"openai-home-v1"}
    assert limits["openai-home-v1"].currency == "KRW"
    assert limits["openai-home-v1"].daily_amount == Decimal("1000")
    assert limits["openai-home-v1"].monthly_amount == Decimal("10000")
