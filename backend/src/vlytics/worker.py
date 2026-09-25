"""Persistent worker process entrypoint."""

import argparse
import asyncio
import logging
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine

from vlytics.config import (
    OperationalConfig,
    OperationalConfigError,
    Settings,
    get_settings,
    load_operational_config,
)
from vlytics.engine.evaluation import DatabaseEvaluationTicker, evaluation_handlers
from vlytics.engine.orchestrator import (
    PredictionOrchestrator,
    ProviderBinding,
    SnapshotFactory,
    StatisticalBinding,
    scheduler_handlers,
)
from vlytics.engine.prediction_validation import JointScoreResolver
from vlytics.engine.providers import (
    BudgetAccount,
    BudgetLedger,
    LiveProviderPlan,
    ProviderBudgetLimits,
    ProviderName,
    build_live_prediction_providers,
    build_live_provider_plan,
    load_variant_registry,
)
from vlytics.ops.repositories.jobs import JobRepository
from vlytics.ops.runtime import (
    DatabaseFeatureSnapshotFactory,
    database_preflight,
    disabled_source_handlers,
    ensure_provider_bindings,
    ensure_statistical_binding,
    local_budget,
    prediction_schema,
    source_collection_enabled,
)
from vlytics.ops.scheduler import (
    DurableScheduler,
    JobHandler,
    LiveDryRunEvidence,
    PostgresScheduleReader,
    PredictionJobDispatcher,
    PredictionKind,
    PredictionVariantSpec,
    ScheduleLifecycleCoordinator,
    SchedulePlanner,
    SchedulerDispatcher,
    SchedulerTiming,
    ScheduleService,
    load_live_dry_run_evidence,
    validate_live_scheduler_activation,
)
from vlytics.storage.database import create_database_engine

logger = logging.getLogger(__name__)


class WorkerDispatcher(Protocol):
    def run_once(self, *, now: datetime) -> int: ...


class SchedulerTicker(Protocol):
    def run_once(self, *, now: datetime) -> int: ...


class Worker:
    """Persistent worker with an injectable durable job dispatcher."""

    def __init__(
        self,
        settings: Settings,
        operational_config: OperationalConfig | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        dispatcher: WorkerDispatcher | None = None,
        scheduler: SchedulerTicker | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._dispatcher = dispatcher
        self._scheduler = scheduler
        self._now = now
        self._operational_config = operational_config or load_operational_config(
            settings, environ=environ, component="worker"
        )

    async def run_once(self) -> int:
        """Lease and execute one registered job."""

        if self._dispatcher is None:
            raise OperationalConfigError("worker dispatcher is not configured")
        now = self._now()
        if self._scheduler is not None:
            self._scheduler.run_once(now=now)
        return self._dispatcher.run_once(now=now)

    async def run(self) -> None:
        """Poll continuously until the process is cancelled."""

        logger.info("Vlytics worker started")
        while True:
            await self.run_once()
            await asyncio.sleep(self._settings.worker_poll_seconds)


def build_worker_dispatcher(
    queue: JobRepository,
    orchestrator: PredictionOrchestrator,
    sync_handlers: Mapping[str, JobHandler],
    *,
    lease_owner: str,
    operational_config: OperationalConfig,
    dry_run_evidence: LiveDryRunEvidence | None = None,
    timing: SchedulerTiming | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SchedulerDispatcher:
    """Combine source-sync and engine handlers in the worker's durable dispatcher."""

    handlers: dict[str, Any] = dict(sync_handlers)
    handlers.update(scheduler_handlers(orchestrator))
    resolved_timing = timing or SchedulerTiming.from_operational_config(operational_config)
    if operational_config.live_operations_enabled:
        validated_timing = validate_live_scheduler_activation(operational_config, dry_run_evidence)
        if resolved_timing != validated_timing:
            raise OperationalConfigError(
                "worker timing differs from the dry-run operational configuration"
            )
    return SchedulerDispatcher(
        queue,
        handlers,
        lease_owner=lease_owner,
        timing=resolved_timing,
        now=now,
    )


class DatabaseScheduleTicker:
    """Build one short-lived scheduler UoW for every worker poll."""

    def __init__(
        self,
        engine: Engine,
        *,
        variants: Sequence[PredictionVariantSpec],
        timing: SchedulerTiming,
    ) -> None:
        self._engine = engine
        self._variants = tuple(variants)
        self._timing = timing

    def run_once(self, *, now: datetime) -> int:
        with self._engine.begin() as connection:
            queue = JobRepository(connection)
            service = ScheduleService(
                PostgresScheduleReader(connection),
                ScheduleLifecycleCoordinator(connection, jobs=queue),
                DurableScheduler(queue, SchedulePlanner(self._timing)),
                self._variants,
            )
            return service.run_once(now=now)


class DatabaseRuntimeTicker:
    """Run schedule planning and result-evaluation discovery on every poll."""

    def __init__(
        self,
        schedule: DatabaseScheduleTicker,
        evaluation: DatabaseEvaluationTicker,
    ) -> None:
        self._schedule = schedule
        self._evaluation = evaluation

    def run_once(self, *, now: datetime) -> int:
        scheduled = self._schedule.run_once(now=now)
        evaluation = self._evaluation.run_once(now=now)
        return scheduled + evaluation


@dataclass(frozen=True)
class WorkerRuntimeComponents:
    snapshot_factory: SnapshotFactory
    statistical: Mapping[str, StatisticalBinding]
    providers: Mapping[str, ProviderBinding]
    sync_handlers: Mapping[str, JobHandler]
    budget: BudgetAccount
    durable_budget_limits: Mapping[str, ProviderBudgetLimits]
    dry_run_evidence: LiveDryRunEvidence | None = None
    max_concurrency: int = 4
    joint_score_resolver: JointScoreResolver | None = None


def build_runtime_components(
    engine: Engine,
    operational_config: OperationalConfig,
    *,
    environ: Mapping[str, str],
    live_plan: LiveProviderPlan | None,
) -> WorkerRuntimeComponents:
    """Construct every production adapter from validated config and the DB boundary."""

    if source_collection_enabled(operational_config.values):
        raise OperationalConfigError(
            "OP-001 live source collection is enabled but no approved KOVO transport exists"
        )
    statistical_binding, statistical_runner = ensure_statistical_binding(engine)
    providers: Mapping[str, ProviderBinding] = {}
    durable_budget_limits: Mapping[str, ProviderBudgetLimits] = {}
    if live_plan is not None:
        live_providers = build_live_prediction_providers(
            live_plan,
            environ=environ,
            prediction_schema=prediction_schema(),
        )
        providers = ensure_provider_bindings(engine, live_plan, live_providers)
        durable_budget_limits = {
            item.variant.variant_id: ProviderBudgetLimits(
                daily_amount=item.daily_budget,
                monthly_amount=item.monthly_budget,
                daily_calls=item.daily_call_limit,
                monthly_calls=item.monthly_call_limit,
                currency=live_plan.budget_currency,
            )
            for item in live_plan.providers
        }
    sync_handlers = disabled_source_handlers()
    sync_handlers.update(evaluation_handlers(engine))
    return WorkerRuntimeComponents(
        snapshot_factory=DatabaseFeatureSnapshotFactory(engine),
        statistical={"statistical-joint-v1": statistical_binding},
        providers=providers,
        sync_handlers=sync_handlers,
        budget=BudgetLedger(local_budget()),
        durable_budget_limits=durable_budget_limits,
        joint_score_resolver=statistical_runner.resolve,
    )


def build_production_worker(
    settings: Settings,
    operational_config: OperationalConfig,
    *,
    environ: Mapping[str, str],
    components: WorkerRuntimeComponents | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Worker:
    """Assemble the real DB scheduler and worker, failing closed on missing adapters."""

    database_config = operational_config.values.get("database")
    if not isinstance(database_config, dict):
        raise OperationalConfigError("worker database configuration is missing")
    database_url_env = database_config.get("connection_url_env")
    if not isinstance(database_url_env, str) or not database_url_env.strip():
        raise OperationalConfigError("worker database connection_url_env is missing")
    configured_database_url = environ.get(database_url_env)
    if not configured_database_url:
        raise OperationalConfigError(
            f"worker database URL environment variable {database_url_env} is missing"
        )
    if configured_database_url != settings.database_url:
        raise OperationalConfigError("worker database URL differs from resolved Settings")
    engine = create_database_engine(settings)
    try:
        database_preflight(engine)
    except Exception as error:
        engine.dispose()
        if isinstance(error, OperationalConfigError):
            raise
        raise OperationalConfigError("worker database preflight failed") from error
    registry_path = Path(__file__).resolve().parents[3] / "config" / "variants.toml"
    registry = load_variant_registry(registry_path)
    timing = SchedulerTiming.from_operational_config(operational_config)
    live_plan: LiveProviderPlan | None = None
    if operational_config.live_operations_enabled:
        dry_run_evidence = (
            components.dry_run_evidence
            if components is not None and components.dry_run_evidence is not None
            else load_live_dry_run_evidence(
                operational_config,
                settings.live_dry_run_evidence_path,
                now=now(),
            )
        )
        validate_live_scheduler_activation(
            operational_config,
            dry_run_evidence,
        )
        live_plan = build_live_provider_plan(
            operational_config,
            registry,
            environ=environ,
        )
    if components is None:
        try:
            components = build_runtime_components(
                engine,
                operational_config,
                environ=environ,
                live_plan=live_plan,
            )
        except Exception as error:
            engine.dispose()
            if isinstance(error, OperationalConfigError):
                raise
            raise OperationalConfigError(
                "worker runtime component initialization failed"
            ) from error
    if "mirror.pre_cutoff_sync" not in components.sync_handlers:
        engine.dispose()
        raise OperationalConfigError("worker pre-cutoff source sync handler is not configured")
    provider_names = {binding.provider.provider_name for binding in components.providers.values()}
    if live_plan is not None and provider_names != set(ProviderName):
        engine.dispose()
        raise OperationalConfigError(
            "worker requires independent openai, anthropic, and google providers"
        )
    if not components.statistical:
        engine.dispose()
        raise OperationalConfigError("worker statistical prediction runner is not configured")
    if live_plan is not None:
        expected_keys = {item.variant.variant_id for item in live_plan.providers}
        if set(components.providers) != expected_keys:
            engine.dispose()
            raise OperationalConfigError("worker provider bindings differ from the live registry")
        durable_budget_limits = {
            item.variant.variant_id: ProviderBudgetLimits(
                daily_amount=item.daily_budget,
                monthly_amount=item.monthly_budget,
                daily_calls=item.daily_call_limit,
                monthly_calls=item.monthly_call_limit,
                currency=live_plan.budget_currency,
            )
            for item in live_plan.providers
        }
    else:
        durable_budget_limits = dict(components.durable_budget_limits)
    variants = tuple(
        PredictionVariantSpec(binding.variant_id, key, PredictionKind.STATISTICAL)
        for key, binding in components.statistical.items()
    ) + tuple(
        PredictionVariantSpec(binding.variant_id, key, PredictionKind.PROVIDER)
        for key, binding in components.providers.items()
    )
    orchestrator = PredictionOrchestrator(
        engine,
        snapshot_factory=components.snapshot_factory,
        statistical=components.statistical,
        providers=components.providers,
        budget=components.budget,
        durable_budget_limits=durable_budget_limits,
        timing=timing,
        now=now,
        joint_score_resolver=components.joint_score_resolver,
    )
    handlers: dict[str, JobHandler] = dict(components.sync_handlers)
    handlers.update(scheduler_handlers(orchestrator))
    lease_owner = f"{socket.gethostname()}:{os.getpid()}"
    dispatcher = PredictionJobDispatcher(
        engine,
        handlers,
        lease_owner=lease_owner,
        timing=timing,
        max_concurrency=components.max_concurrency,
        now=now,
    )
    schedule_ticker = DatabaseScheduleTicker(engine, variants=variants, timing=timing)
    return Worker(
        settings,
        operational_config,
        environ=environ,
        dispatcher=dispatcher,
        scheduler=DatabaseRuntimeTicker(schedule_ticker, DatabaseEvaluationTicker(engine)),
        now=now,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Vlytics worker")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
    runtime_components: WorkerRuntimeComponents | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Run the worker from the shared backend artifact."""

    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    resolved_settings = settings or get_settings()
    try:
        resolved_environ = environ if environ is not None else os.environ
        operational_config = load_operational_config(
            resolved_settings,
            environ=resolved_environ,
            component="worker",
        )
        worker = build_production_worker(
            resolved_settings,
            operational_config,
            environ=resolved_environ,
            components=runtime_components,
            now=now,
        )
    except (OperationalConfigError, OSError, ValueError) as error:
        logger.error("Worker configuration rejected: %s", error)
        return 2

    try:
        if args.once:
            asyncio.run(worker.run_once())
        else:
            asyncio.run(worker.run())
    except KeyboardInterrupt:
        logger.info("Vlytics worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
