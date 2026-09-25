"""Contract tests for independent fake GPT, Claude, and Gemini adapters."""

from __future__ import annotations

import json
import time
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from vlytics.config import OperationalConfig, OperationalConfigError
from vlytics.engine.features.definitions import (
    FEATURE_VERSION,
    MATCHUP_FEATURE_KEYS,
    TEAM_FEATURE_KEYS,
)
from vlytics.engine.features.models import (
    AvailabilityPolicy,
    FeatureLineage,
    FeatureSnapshot,
    FeatureStatus,
    FeatureValue,
    LineupStatus,
    MissingReason,
    compute_snapshot_sha256,
)
from vlytics.engine.providers import (
    AI_OUTPUT_SCHEMA,
    AnthropicPredictionProvider,
    AttemptStatus,
    BudgetLedger,
    FailureCode,
    FakeProviderTransport,
    GooglePredictionProvider,
    OpenAIPredictionProvider,
    PredictionContextV1,
    ProviderName,
    ProviderTimeoutError,
    ProviderUsage,
    ProviderVariant,
    VariantRegistry,
    VersionPolicy,
    build_live_provider_plan,
    load_variant_registry,
    run_provider_batch,
)
from vlytics.engine.providers.base import PROMPT_TEMPLATE_HASH
from vlytics.engine.providers.models import sha256_bytes
from vlytics.engine.repositories.attempts import PredictionAttemptRecord

ROOT = Path(__file__).resolve().parents[3]
PREDICTION_SCHEMA = json.loads(
    (ROOT / "contracts" / "prediction-v1.schema.json").read_text(encoding="utf-8")
)
PROVIDER_TYPES = {
    ProviderName.OPENAI: OpenAIPredictionProvider,
    ProviderName.ANTHROPIC: AnthropicPredictionProvider,
    ProviderName.GOOGLE: GooglePredictionProvider,
}
NOW = datetime(2026, 9, 20, 9, tzinfo=UTC)


def _feature_value(key: str) -> FeatureValue:
    return FeatureValue(
        definition=key,
        status=FeatureStatus.UNKNOWN,
        value=None,
        numerator=None,
        denominator=None,
        sample_size=0,
        missing_reason=MissingReason.MISSING_INPUT,
        lineage=FeatureLineage(),
    )


def _snapshot() -> FeatureSnapshot:
    snapshot = FeatureSnapshot(
        feature_version=FEATURE_VERSION,
        availability_policy=AvailabilityPolicy.LIVE_PROSPECTIVE,
        target_match_id="match-1",
        schedule_revision_id="schedule-1",
        cutoff_at=NOW,
        captured_at=NOW,
        lineup_status=LineupStatus.UNKNOWN,
        team_features={
            side: {key: _feature_value(key) for key in TEAM_FEATURE_KEYS}
            for side in ("home", "away")
        },
        matchup_features={key: _feature_value(key) for key in MATCHUP_FEATURE_KEYS},
        players={},
        lineage=FeatureLineage(),
        sha256="0" * 64,
    )
    return replace(snapshot, sha256=compute_snapshot_sha256(snapshot))


def _context() -> PredictionContextV1:
    return PredictionContextV1.from_feature_snapshot("snapshot-1", _snapshot())


def _variant(
    provider: ProviderName,
    *,
    max_input_tokens: int = 100_000,
    max_output_tokens: int = 100,
    version_policy: VersionPolicy = VersionPolicy.VERIFY_RESOLVED_MODEL_ID,
) -> ProviderVariant:
    requested = f"{provider.value}-alias-test"
    pinned = f"{provider.value}-resolved-2026-09-20"
    if version_policy is VersionPolicy.IMMUTABLE_MODEL_ID:
        requested = pinned
    return ProviderVariant(
        variant_id=f"{provider.value}-independent-test-v1",
        provider=provider,
        requested_model_id=requested,
        pinned_model_version=pinned,
        version_policy=version_policy,
        prompt_version="independent-volleyball-v1",
        prompt_hash=PROMPT_TEMPLATE_HASH,
        distribution_version="ai-direct-set-v1",
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        input_cost_per_million=Decimal("1"),
        output_cost_per_million=Decimal("2"),
        enabled=True,
        op003_resolved=True,
    )


def _core_output(
    *,
    probabilities: list[float] | None = None,
    raw_home_probability: float | None = None,
) -> dict[str, Any]:
    values = probabilities or [0.18, 0.17, 0.15, 0.15, 0.17, 0.18]
    home_probability = sum(values[:3]) if raw_home_probability is None else raw_home_probability
    return {
        "home_win_probability": home_probability,
        "set_score_probabilities": [
            {"outcome": outcome, "probability": probability}
            for outcome, probability in zip(
                ("3:0", "3:1", "3:2", "2:3", "1:3", "0:3"),
                values,
                strict=True,
            )
        ],
        "rationale": "The frozen recent-form features are balanced.",
        "risk_factors": ["lineup is not confirmed"],
        "confidence_note": "Moderate uncertainty.",
    }


def _response(
    provider: ProviderName,
    variant: ProviderVariant,
    *,
    output_text: str | None = None,
    resolved_model: str | None = None,
    refusal: bool = False,
    input_tokens: int = 10,
    output_tokens: int = 5,
    google_prompt_block: bool = False,
) -> bytes:
    text = output_text if output_text is not None else json.dumps(_core_output())
    model = resolved_model or variant.pinned_model_version
    if provider is ProviderName.OPENAI:
        content = (
            [{"type": "refusal", "refusal": "external refusal text"}]
            if refusal
            else [{"type": "output_text", "text": text}]
        )
        document = {
            "id": "openai-request-1",
            "model": model,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "output": [{"type": "message", "content": content}],
        }
    elif provider is ProviderName.ANTHROPIC:
        content = (
            [{"type": "refusal", "refusal": "external refusal text"}]
            if refusal
            else [{"type": "text", "text": text}]
        )
        document = {
            "id": "anthropic-request-1",
            "model": model,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "stop_reason": "refusal" if refusal else "end_turn",
            "content": content,
        }
    else:
        document = {
            "responseId": "google-request-1",
            "modelVersion": model,
            "usageMetadata": {
                "promptTokenCount": input_tokens,
                "candidatesTokenCount": output_tokens,
            },
        }
        if google_prompt_block:
            document["promptFeedback"] = {"blockReason": "SAFETY"}
        else:
            document["candidates"] = [
                {
                    "finishReason": "SAFETY" if refusal else "STOP",
                    "content": {"parts": [{"text": text}]},
                }
            ]
    return json.dumps(document, separators=(",", ":")).encode()


def _providers() -> tuple[list[Any], dict[ProviderName, FakeProviderTransport]]:
    providers = []
    transports = {}
    for provider_name, provider_type in PROVIDER_TYPES.items():
        variant = _variant(provider_name)
        transport = FakeProviderTransport(response_body=_response(provider_name, variant))
        providers.append(provider_type(variant, transport, PREDICTION_SCHEMA))  # type: ignore[abstract]
        transports[provider_name] = transport
    return providers, transports


def test_three_adapters_receive_the_identical_typed_snapshot_and_record_lineage() -> None:
    context = _context()
    providers, transports = _providers()
    results = run_provider_batch(
        providers,
        context,
        timeout_ms=2_000,
        budget=BudgetLedger(Decimal("1")),
    )

    assert set(results) == set(ProviderName)
    assert {result.status for result in results.values()} == {AttemptStatus.SUCCEEDED}
    for provider_name, result in results.items():
        invocation = transports[provider_name].invocations[0]
        assert invocation.snapshot_bytes == context.snapshot_bytes
        assert invocation.snapshot_sha256 == context.snapshot_sha256
        assert result.request_hash == sha256_bytes(invocation.request_body)
        assert result.response_hash == sha256_bytes(transports[provider_name].response_body or b"")
        assert result.resolved_model_id == _variant(provider_name).pinned_model_version
        assert result.cost == Decimal("0.00002")
        assert result.prompt_hash == PROMPT_TEMPLATE_HASH
        assert result.output is not None
        provenance = result.output["target_provenance"]["winner"]
        assert provenance["probability_source"] == "ai_direct"
        assert provenance["prompt_hash"] == PROMPT_TEMPLATE_HASH
        request = json.loads(invocation.request_body)
        assert not {"tools", "tool_choice", "web_search"} & set(request)
        if provider_name is ProviderName.OPENAI:
            assert request["model"] == _variant(provider_name).requested_model_id
            assert request["text"]["format"]["type"] == "json_schema"
            assert request["text"]["format"]["strict"] is True
            assert "response_format" not in request
        elif provider_name is ProviderName.ANTHROPIC:
            assert request["model"] == _variant(provider_name).requested_model_id
            assert request["output_config"]["format"]["type"] == "json_schema"
        else:
            assert "model" not in request
            generation = request["generationConfig"]
            assert generation["responseMimeType"] == "application/json"
            assert generation["responseJsonSchema"] == AI_OUTPUT_SCHEMA


def test_context_has_no_public_arbitrary_json_constructor_and_enforces_allowlist() -> None:
    with pytest.raises(TypeError):
        PredictionContextV1(  # type: ignore[call-arg]
            snapshot_id="unsafe",
            snapshot_sha256="0" * 64,
            snapshot_document={"market": {}},
        )
    snapshot = _snapshot()
    unsafe = replace(
        snapshot,
        team_features={
            **snapshot.team_features,
            "market": {key: _feature_value(key) for key in TEAM_FEATURE_KEYS},
        },
    )
    with pytest.raises(ValueError, match="exactly home and away"):
        PredictionContextV1.from_feature_snapshot("unsafe", unsafe)
    wrong_version = replace(snapshot, feature_version="feature-v2")
    with pytest.raises(ValueError, match="feature-v1"):
        PredictionContextV1.from_feature_snapshot("unsafe", wrong_version)


def test_hung_provider_times_out_without_blocking_other_providers() -> None:
    context = _context()
    providers, _ = _providers()
    openai = providers[0]

    def hang(_: object) -> bytes:
        time.sleep(0.25)
        return _response(ProviderName.OPENAI, openai.variant)

    openai.transport = FakeProviderTransport(handler=hang)
    started = time.monotonic()
    results = run_provider_batch(
        providers,
        context,
        timeout_ms=30,
        budget=BudgetLedger(Decimal("1")),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert results[ProviderName.OPENAI].failure_code is FailureCode.TIMEOUT
    assert results[ProviderName.ANTHROPIC].status is AttemptStatus.SUCCEEDED
    assert results[ProviderName.GOOGLE].status is AttemptStatus.SUCCEEDED


def test_timeout_and_invalid_response_charge_budget_after_send() -> None:
    variant = _variant(ProviderName.OPENAI)
    timeout_budget = BudgetLedger(Decimal("1"))

    def slow_response(_: object) -> bytes:
        time.sleep(0.1)
        return b"{}"

    timeout = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(handler=slow_response),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=10, budget=timeout_budget)
    invalid_budget = BudgetLedger(Decimal("1"))
    invalid = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(response_body=b"{"),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=invalid_budget)
    reported_budget = BudgetLedger(Decimal("0.15"))
    reported = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(
            error=ProviderTimeoutError(usage=ProviderUsage(input_tokens=200_000, output_tokens=100))
        ),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=reported_budget)

    assert timeout.failure_code is FailureCode.TIMEOUT
    assert timeout.cost == variant.maximum_call_cost
    assert timeout_budget.spent == variant.maximum_call_cost
    assert invalid.failure_code is FailureCode.INVALID_JSON
    assert invalid.cost == variant.maximum_call_cost
    assert invalid_budget.spent == variant.maximum_call_cost
    assert reported.failure_code is FailureCode.TIMEOUT
    assert reported.cost == Decimal("0.2002")
    assert reported_budget.spent == Decimal("0.2002")


def test_actual_cost_can_exceed_reservation_and_blocks_future_calls() -> None:
    variant = _variant(ProviderName.OPENAI)
    transport = FakeProviderTransport(
        response_body=_response(
            ProviderName.OPENAI,
            variant,
            input_tokens=200_000,
            output_tokens=100,
        )
    )
    provider = OpenAIPredictionProvider(variant, transport, PREDICTION_SCHEMA)
    budget = BudgetLedger(Decimal("0.15"))

    first = provider.predict(_context(), timeout_ms=100, budget=budget)
    second = provider.predict(_context(), timeout_ms=100, budget=budget)

    assert first.failure_code is FailureCode.PROVIDER_ERROR
    assert first.cost == Decimal("0.2002")
    assert budget.spent == Decimal("0.2002")
    assert second.failure_code is FailureCode.BUDGET_SKIPPED
    assert len(transport.invocations) == 1


def test_input_token_bound_is_checked_before_send() -> None:
    variant = _variant(ProviderName.OPENAI, max_input_tokens=1)
    transport = FakeProviderTransport(response_body=_response(ProviderName.OPENAI, variant))
    result = OpenAIPredictionProvider(
        variant,
        transport,
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))

    assert result.failure_code is FailureCode.INPUT_TOO_LARGE
    assert result.status is AttemptStatus.SKIPPED
    assert result.request_hash
    assert not transport.invocations


@pytest.mark.parametrize(
    ("output_text", "expected"),
    [
        ("{", FailureCode.INVALID_JSON),
        (
            json.dumps(_core_output(probabilities=[0.2, 0.2, 0.2, 0.2, 0.2, 0.2])),
            FailureCode.INVALID_PROBABILITY_SUM,
        ),
        (
            json.dumps(_core_output()).replace(
                '"home_win_probability": 0.5',
                '"home_win_probability": NaN',
            ),
            FailureCode.NON_FINITE_PROBABILITY,
        ),
    ],
)
def test_invalid_json_sum_and_nan_have_separate_states(
    output_text: str,
    expected: FailureCode,
) -> None:
    variant = _variant(ProviderName.OPENAI)
    result = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(
            response_body=_response(
                ProviderName.OPENAI,
                variant,
                output_text=output_text,
            )
        ),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))
    assert result.failure_code is expected
    assert result.cost == Decimal("0.00002")


def test_refusal_alias_drift_and_unverified_alias_are_distinct() -> None:
    variant = _variant(ProviderName.OPENAI)
    refusal = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(response_body=_response(ProviderName.OPENAI, variant, refusal=True)),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))
    drift = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(
            response_body=_response(
                ProviderName.OPENAI,
                variant,
                resolved_model="unexpected-version",
            )
        ),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))
    unverified = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(
            response_body=_response(
                ProviderName.OPENAI,
                variant,
                resolved_model=variant.requested_model_id,
            )
        ),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))

    assert refusal.failure_code is FailureCode.REFUSAL
    assert refusal.detail == "provider refused the prediction"
    assert drift.failure_code is FailureCode.ALIAS_DRIFT
    assert unverified.failure_code is FailureCode.VERSION_UNVERIFIED


@pytest.mark.parametrize("provider_name", list(ProviderName))
def test_response_decoders_skip_non_text_items_and_join_text_parts(
    provider_name: ProviderName,
) -> None:
    variant = _variant(provider_name)
    output = json.dumps(_core_output(), separators=(",", ":"))
    midpoint = len(output) // 2
    if provider_name is ProviderName.OPENAI:
        document = {
            "id": "request-1",
            "model": variant.pinned_model_version,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": output[:midpoint]},
                        {"type": "output_text", "text": output[midpoint:]},
                    ],
                },
            ],
        }
    elif provider_name is ProviderName.ANTHROPIC:
        document = {
            "id": "request-1",
            "model": variant.pinned_model_version,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "not persisted"},
                {"type": "text", "text": output[:midpoint]},
                {"type": "text", "text": output[midpoint:]},
            ],
        }
    else:
        document = {
            "responseId": "request-1",
            "modelVersion": variant.pinned_model_version,
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {"thoughtSignature": "not-persisted"},
                            {"text": output[:midpoint]},
                            {"text": output[midpoint:]},
                        ]
                    },
                }
            ],
        }
    body = json.dumps(document, separators=(",", ":")).encode()
    provider = PROVIDER_TYPES[provider_name](
        variant,
        FakeProviderTransport(response_body=body),
        PREDICTION_SCHEMA,
    )

    metadata = provider.parse_response_metadata(body)
    parsed = provider.parse_response(body, metadata)

    assert parsed.output_text == output
    assert parsed.refusal_reason is None


def test_google_prompt_block_is_read_before_missing_candidate_content() -> None:
    variant = _variant(ProviderName.GOOGLE)
    result = GooglePredictionProvider(
        variant,
        FakeProviderTransport(
            response_body=_response(
                ProviderName.GOOGLE,
                variant,
                google_prompt_block=True,
            )
        ),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))
    assert result.failure_code is FailureCode.REFUSAL


def test_external_exception_details_are_sanitized() -> None:
    variant = _variant(ProviderName.OPENAI)
    result = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(error=RuntimeError("secret-token-and-request-fragment")),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))

    assert result.failure_code is FailureCode.PROVIDER_ERROR
    assert result.detail == "provider transport failed"
    assert "secret-token" not in (result.detail or "")


def test_caller_controlled_fake_marker_cannot_enable_transport() -> None:
    class SpoofedTransport:
        is_fake = True

        def send(self, _: object) -> bytes:
            raise AssertionError("spoofed transport must never be called")

    variant = _variant(ProviderName.OPENAI)
    result = OpenAIPredictionProvider(
        variant,
        SpoofedTransport(),  # type: ignore[arg-type]
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))
    assert result.failure_code is FailureCode.CONFIG_UNRESOLVED


def test_unresolved_fixed_registry_has_verified_prompt_but_fails_closed() -> None:
    registry = load_variant_registry(ROOT / "config" / "variants.toml")
    assert len(registry.variants) == 3
    assert all(variant.prompt_hash == PROMPT_TEMPLATE_HASH for variant in registry.variants)
    assert not registry.live_ready
    variant = registry.variants[0]
    transport = FakeProviderTransport(response_body=b"{}")
    result = OpenAIPredictionProvider(
        variant,
        transport,
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("0")))
    assert result.failure_code is FailureCode.CONFIG_UNRESOLVED
    assert result.request_hash
    assert not transport.invocations


def test_live_plan_requires_operational_config_pricing_budgets_limits_and_secrets() -> None:
    variants = tuple(_variant(provider) for provider in ProviderName)
    registry = VariantRegistry(
        schema_version="1",
        variants=variants,
        budget_resolved=True,
        daily_budget_amount=Decimal("6"),
        monthly_budget_amount=Decimal("60"),
    )
    with (ROOT / "config" / "example.toml").open("rb") as stream:
        values = tomllib.load(stream)
    values["environment"] = "production"
    values["live_operations_enabled"] = True
    deployment = values["deployment"]
    assert isinstance(deployment, dict)
    deployment.update(
        {
            "live_enabled": True,
            "host_class": "managed_vm_and_database",
            "host_provider": "synthetic-provider",
            "host_region": "synthetic-region",
            "monthly_cost_limit": 100,
            "cost_currency": "USD",
            "network_access": "private_network",
            "operator_auth_method": "mutual_tls",
        }
    )
    backup = values["backup"]
    assert isinstance(backup, dict)
    backup.update(
        {
            "enabled": True,
            "strategy": "synthetic-backup",
            "interval_hours": 6,
            "retention_days": 7,
            "rpo_minutes": 60,
            "rto_minutes": 120,
        }
    )
    alerting = values["alerting"]
    assert isinstance(alerting, dict)
    alerting.update({"enabled": True, "channel": "synthetic-alert"})
    ai = values["ai"]
    assert isinstance(ai, dict)
    ai.update({"live_calls_enabled": True, "budget_currency": "USD"})
    secrets: dict[str, str] = {
        "VLYTICS_DATABASE_URL": "synthetic-database-secret",
        "VLYTICS_OPERATOR_AUTH_SECRET": "synthetic-operator-secret",
        "VLYTICS_BACKUP_CREDENTIAL": "synthetic-backup-secret",
        "VLYTICS_ALERT_DESTINATION": "synthetic-alert-secret",
    }
    for variant in variants:
        secret_name = f"TEST_{variant.provider.value.upper()}_KEY"
        secrets[secret_name] = "synthetic-secret"
        ai[variant.provider.value] = {
            "enabled": True,
            "model_id": variant.requested_model_id,
            "version_policy": variant.version_policy.value,
            "pinned_model_version": variant.pinned_model_version,
            "api_key_env": secret_name,
            "daily_budget_amount": 1,
            "monthly_budget_amount": 10,
            "max_calls_per_match": 1,
            "daily_call_limit": 10,
            "monthly_call_limit": 100,
            "max_input_tokens_per_call": 100_000,
            "max_output_tokens_per_call": 100,
        }
    config = OperationalConfig(
        values=values,
        source_path=ROOT / "synthetic.toml",
        schema_path=ROOT / "contracts" / "config.schema.json",
    )

    plan = build_live_provider_plan(config, registry, environ=secrets)
    assert len(plan.providers) == 3
    assert all(provider.variant.operational for provider in plan.providers)
    missing = dict(secrets)
    del missing["TEST_OPENAI_KEY"]
    with pytest.raises(OperationalConfigError, match="missing environment variable"):
        build_live_provider_plan(config, registry, environ=missing)


def test_all_result_classes_map_to_prediction_attempt_rows() -> None:
    variant = _variant(ProviderName.OPENAI)
    success_provider = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(response_body=_response(ProviderName.OPENAI, variant)),
        PREDICTION_SCHEMA,
    )
    success = success_provider.predict(
        _context(),
        timeout_ms=100,
        budget=BudgetLedger(Decimal("1")),
    )
    timeout = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(error=ProviderTimeoutError()),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))
    skipped = OpenAIPredictionProvider(
        _variant(ProviderName.OPENAI, max_input_tokens=1),
        FakeProviderTransport(response_body=b"{}"),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))
    budget_skipped = success_provider.predict(
        _context(),
        timeout_ms=100,
        budget=BudgetLedger(Decimal("0")),
    )
    failed = OpenAIPredictionProvider(
        variant,
        FakeProviderTransport(response_body=b"{"),
        PREDICTION_SCHEMA,
    ).predict(_context(), timeout_ms=100, budget=BudgetLedger(Decimal("1")))

    expected = (
        (success, "succeeded"),
        (timeout, "timed_out"),
        (skipped, "skipped"),
        (budget_skipped, "budget_skipped"),
        (failed, "failed"),
    )
    for result, status in expected:
        record = PredictionAttemptRecord.from_result(
            result,
            job_id=uuid4(),
            snapshot_id=uuid4(),
            variant_id=uuid4(),
            started_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
        )
        assert record.status == status
        assert record.request_hash == result.request_hash
        assert record.usage_json["prompt_hash"] == PROMPT_TEMPLATE_HASH
        assert record.usage_json["cost_amount"] == str(result.cost)

    preflight_skips = {
        FailureCode.CONFIG_UNRESOLVED,
        FailureCode.INPUT_POLICY_VIOLATION,
        FailureCode.INPUT_TOO_LARGE,
        FailureCode.BUDGET_SKIPPED,
    }
    for failure_code in FailureCode:
        status = AttemptStatus.SKIPPED if failure_code in preflight_skips else AttemptStatus.FAILED
        result = replace(failed, failure_code=failure_code, status=status)
        record = PredictionAttemptRecord.from_result(
            result,
            job_id=uuid4(),
            snapshot_id=uuid4(),
            variant_id=uuid4(),
            started_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
        )
        assert record.error_code == failure_code.value
        if failure_code is FailureCode.TIMEOUT:
            assert record.status == "timed_out"
        elif failure_code is FailureCode.BUDGET_SKIPPED:
            assert record.status == "budget_skipped"
        elif failure_code in preflight_skips:
            assert record.status == "skipped"
        else:
            assert record.status == "failed"

    migration = (
        ROOT / "backend" / "migrations" / "0011_prediction_attempt_provider_outcomes.sql"
    ).read_text(encoding="utf-8")
    assert "'skipped'" in migration
