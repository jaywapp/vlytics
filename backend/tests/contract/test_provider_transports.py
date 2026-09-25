"""Offline contract tests for pinned production provider HTTP transports."""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from vlytics.engine.providers import (
    AnthropicProviderTransport,
    GoogleProviderTransport,
    OpenAIProviderTransport,
    ProviderHTTPError,
    ProviderName,
    ProviderResponseTooLargeError,
    ProviderTimeoutError,
    ProviderTransportLimits,
    ProviderVariant,
    RuntimeProviderPlan,
    VersionPolicy,
    build_live_prediction_providers,
)
from vlytics.engine.providers.base import PROMPT_TEMPLATE_HASH
from vlytics.engine.providers.models import ProviderInvocation
from vlytics.engine.providers.operational import LiveProviderPlan

API_KEY = "test-secret-api-key"
MODEL_IDS = {
    ProviderName.OPENAI: "gpt-pinned-test",
    ProviderName.ANTHROPIC: "claude-pinned-test",
    ProviderName.GOOGLE: "models/gemini-pinned-test",
}


def _invocation(provider: ProviderName, body: dict[str, Any]) -> ProviderInvocation:
    return ProviderInvocation(
        provider=provider,
        variant_id=f"{provider.value}-test",
        snapshot_sha256="a" * 64,
        snapshot_bytes=b"{}",
        request_body=json.dumps(body, separators=(",", ":")).encode(),
        estimated_input_tokens=2,
        timeout_ms=5_000,
    )


def _transport(
    provider: ProviderName,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    model_id: str | None = None,
    limits: ProviderTransportLimits | None = None,
) -> OpenAIProviderTransport | AnthropicProviderTransport | GoogleProviderTransport:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    arguments = {
        "api_key": API_KEY,
        "model_id": model_id or MODEL_IDS[provider],
        "client": client,
        "limits": limits,
    }
    if provider is ProviderName.OPENAI:
        return OpenAIProviderTransport(**arguments)
    if provider is ProviderName.ANTHROPIC:
        return AnthropicProviderTransport(**arguments)
    return GoogleProviderTransport(**arguments)


@pytest.mark.parametrize("provider", list(ProviderName))
def test_live_transports_pin_https_request_shape_and_auth_header(provider: ProviderName) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b'{"ok":true}')

    transport = _transport(provider, handler)
    body = {} if provider is ProviderName.GOOGLE else {"model": MODEL_IDS[provider]}

    assert transport.send(_invocation(provider, body)) == b'{"ok":true}'
    request = captured[0]
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.query == b""
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == body
    if provider is ProviderName.OPENAI:
        assert request.url == httpx.URL("https://api.openai.com/v1/responses")
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        assert "x-api-key" not in request.headers
    elif provider is ProviderName.ANTHROPIC:
        assert request.url == httpx.URL("https://api.anthropic.com/v1/messages")
        assert request.headers["x-api-key"] == API_KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
    else:
        assert request.url == httpx.URL(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-pinned-test:generateContent"
        )
        assert request.headers["x-goog-api-key"] == API_KEY
        assert "key" not in request.url.params


def test_google_model_is_one_encoded_path_segment_and_cannot_inject_host_or_query() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"{}")

    transport = _transport(
        ProviderName.GOOGLE,
        handler,
        model_id="models/gemini/../../x?key=stolen#fragment",
    )
    transport.send(_invocation(ProviderName.GOOGLE, {}))

    url = captured[0].url
    assert url.host == "generativelanguage.googleapis.com"
    assert url.query == b""
    assert "%2F..%2F..%2Fx%3Fkey%3Dstolen%23fragment" in str(url)
    assert API_KEY not in str(url)


def test_redirect_is_not_followed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://evil.example/steal"})

    transport = _transport(ProviderName.OPENAI, handler)
    with pytest.raises(ProviderHTTPError) as captured:
        transport.send(_invocation(ProviderName.OPENAI, {"model": MODEL_IDS[ProviderName.OPENAI]}))

    assert captured.value.status_code == 307
    assert len(requests) == 1
    assert requests[0].url.host == "api.openai.com"


def test_429_maps_only_safe_structured_metadata_and_retry_after() -> None:
    body = {
        "error": {
            "type": "rate_limit_error",
            "message": "secret-token prompt-fragment",
        }
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "2.5", "x-request-id": "req_safe-123"},
            json=body,
        )

    transport = _transport(ProviderName.ANTHROPIC, handler)
    with pytest.raises(ProviderHTTPError) as captured:
        transport.send(
            _invocation(
                ProviderName.ANTHROPIC,
                {"model": MODEL_IDS[ProviderName.ANTHROPIC]},
            )
        )

    error = captured.value
    assert error.status_code == 429
    assert error.error_code == "rate_limit_error"
    assert error.request_id == "req_safe-123"
    assert error.retry_after_seconds == 2.5
    representation = f"{error!r} {error!s} {transport!r}"
    assert API_KEY not in representation
    assert "secret-token" not in representation
    assert "prompt-fragment" not in representation


def test_network_timeout_is_sanitized_provider_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret-token prompt-fragment", request=request)

    transport = _transport(ProviderName.OPENAI, handler)
    with pytest.raises(ProviderTimeoutError) as captured:
        transport.send(_invocation(ProviderName.OPENAI, {"model": MODEL_IDS[ProviderName.OPENAI]}))

    assert str(captured.value) == "provider request timed out"
    rendered = "".join(traceback.format_exception(captured.value))
    assert "secret-token" not in repr(captured.value)
    assert "secret-token" not in rendered
    assert "prompt-fragment" not in rendered


@pytest.mark.parametrize("use_content_length", [False, True])
def test_response_size_is_bounded_for_streamed_and_declared_lengths(
    use_content_length: bool,
) -> None:
    headers = {"content-length": "64"} if use_content_length else {}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=b"x" * 64)

    transport = _transport(
        ProviderName.GOOGLE,
        handler,
        limits=ProviderTransportLimits(max_response_bytes=16),
    )
    with pytest.raises(ProviderResponseTooLargeError) as captured:
        transport.send(_invocation(ProviderName.GOOGLE, {}))

    assert captured.value.limit_bytes == 16
    assert "xxxxxxxx" not in str(captured.value)


def test_success_body_is_returned_exactly_for_strict_adapter_decoding() -> None:
    malformed = b'{"candidates":['

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=malformed)

    transport = _transport(ProviderName.GOOGLE, handler)
    assert transport.send(_invocation(ProviderName.GOOGLE, {})) == malformed


def _variant(provider: ProviderName) -> ProviderVariant:
    return ProviderVariant(
        variant_id=f"{provider.value}-live-test",
        provider=provider,
        requested_model_id=MODEL_IDS[provider],
        pinned_model_version=MODEL_IDS[provider],
        version_policy=VersionPolicy.IMMUTABLE_MODEL_ID,
        prompt_version="provider-live-v1",
        prompt_hash=PROMPT_TEMPLATE_HASH,
        distribution_version="ai-direct-set-v1",
        max_input_tokens=1_000,
        max_output_tokens=100,
        input_cost_per_million=Decimal("1"),
        output_cost_per_million=Decimal("1"),
        enabled=True,
        op003_resolved=True,
    )


def test_production_factory_builds_only_pinned_live_transports() -> None:
    plans = tuple(
        RuntimeProviderPlan(
            variant=_variant(provider),
            api_key_env=f"TEST_{provider.value.upper()}_KEY",
            daily_budget=Decimal("1"),
            monthly_budget=Decimal("10"),
            max_calls_per_match=1,
            daily_call_limit=10,
            monthly_call_limit=100,
        )
        for provider in ProviderName
    )
    live_plan = LiveProviderPlan(budget_currency="USD", providers=plans)
    secrets = {item.api_key_env: API_KEY for item in plans}

    providers = build_live_prediction_providers(
        live_plan,
        environ=secrets,
        prediction_schema={"type": "object"},
        client_factory=lambda _: httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ),
    )

    assert [type(provider.transport) for provider in providers] == [
        OpenAIProviderTransport,
        AnthropicProviderTransport,
        GoogleProviderTransport,
    ]
    assert all(API_KEY not in repr(provider.transport) for provider in providers)


def test_production_factory_fails_closed_when_secret_is_missing() -> None:
    item = RuntimeProviderPlan(
        variant=_variant(ProviderName.OPENAI),
        api_key_env="MISSING_OPENAI_KEY",
        daily_budget=Decimal("1"),
        monthly_budget=Decimal("10"),
        max_calls_per_match=1,
        daily_call_limit=10,
        monthly_call_limit=100,
    )
    with pytest.raises(ValueError, match="missing provider secret environment variable"):
        build_live_prediction_providers(
            LiveProviderPlan(budget_currency="USD", providers=(item,)),
            environ={},
            prediction_schema={"type": "object"},
        )
