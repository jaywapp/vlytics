"""Failure-isolated execution across independent AI providers."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

from .base import PredictionProvider
from .models import (
    AttemptStatus,
    BudgetAccount,
    FailureCode,
    PredictionAttemptResult,
    PredictionContextV1,
    ProviderName,
)


def run_provider_batch(
    providers: Iterable[PredictionProvider],
    context: PredictionContextV1,
    *,
    timeout_ms: int,
    budget: BudgetAccount,
) -> dict[ProviderName, PredictionAttemptResult]:
    provider_list = tuple(providers)
    names = [provider.provider_name for provider in provider_list]
    if len(names) != len(set(names)):
        raise ValueError("provider batch contains a duplicate provider")
    results: dict[ProviderName, PredictionAttemptResult] = {}
    executor = ThreadPoolExecutor(
        max_workers=max(1, len(provider_list)),
        thread_name_prefix="prediction-provider",
    )
    futures = {
        executor.submit(
            provider.predict,
            context,
            timeout_ms=timeout_ms,
            budget=budget,
        ): provider
        for provider in provider_list
    }
    for future in as_completed(futures):
        provider = futures[future]
        try:
            result = future.result()
        except Exception:
            result = PredictionAttemptResult(
                provider=provider.provider_name,
                variant_id=provider.variant.variant_id,
                status=AttemptStatus.FAILED,
                failure_code=FailureCode.PROVIDER_ERROR,
                snapshot_sha256=context.snapshot_sha256,
                requested_model_id=provider.variant.requested_model_id,
                pinned_model_version=provider.variant.pinned_model_version,
                version_policy=provider.variant.version_policy,
                prompt_version=provider.variant.prompt_version,
                prompt_hash=provider.variant.prompt_hash,
                request_hash=provider.preflight_request_hash(context),
                response_hash=None,
                resolved_model_id=None,
                request_id=None,
                usage=None,
                cost=Decimal("0"),
                latency_ms=0,
                output=None,
                detail="provider execution failed",
            )
        results[provider.provider_name] = result
    executor.shutdown(wait=False, cancel_futures=True)
    return results
