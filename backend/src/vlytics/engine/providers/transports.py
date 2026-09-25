"""Pinned HTTPS transports for the three production model providers."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, final
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from .base import PredictionProvider, ProviderTimeoutError, ProviderTransport
from .models import ProviderInvocation, ProviderName

if TYPE_CHECKING:
    from .operational import LiveProviderPlan

OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
GOOGLE_ENDPOINT_PREFIX = "https://generativelanguage.googleapis.com/v1beta/models/"
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


@dataclass(frozen=True)
class ProviderTransportLimits:
    """Connection limits that are further bounded by each invocation wall timeout."""

    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 60.0
    max_response_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("provider transport timeouts must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("provider response byte limit must be positive")


class ProviderTransportError(RuntimeError):
    """Sanitized transport failure that never retains request or response payloads."""


class ProviderConnectionError(ProviderTransportError):
    def __init__(self) -> None:
        super().__init__("provider connection failed")


class ProviderResponseTooLargeError(ProviderTransportError):
    def __init__(self, *, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        super().__init__("provider response exceeded the configured byte limit")


class ProviderHTTPError(ProviderTransportError):
    """Sanitized non-success response metadata without provider error text or body."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str | None,
        request_id: str | None,
        retry_after_seconds: float | None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"provider HTTP request failed with status {status_code}")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status_code={self.status_code!r}, "
            f"retry_after_seconds={self.retry_after_seconds!r})"
        )


class _PinnedHTTPTransport:
    __slots__ = ("_api_key", "_client", "_endpoint", "_limits", "_model_id")

    provider_name: ProviderName
    allowed_host: str

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        client: httpx.Client | None = None,
        limits: ProviderTransportLimits | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("provider API key must not be blank")
        self._model_id = _validate_model_id(model_id)
        self._api_key = SecretStr(api_key)
        self._limits = limits or ProviderTransportLimits()
        self._endpoint = self._build_endpoint(self._model_id)
        _assert_pinned_endpoint(self._endpoint, self.allowed_host)
        self._client = client or httpx.Client(trust_env=False, follow_redirects=False)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider={self.provider_name.value!r}, "
            "api_key=SecretStr('**********'))"
        )

    def send(self, invocation: ProviderInvocation) -> bytes:
        if invocation.provider is not self.provider_name:
            raise ProviderConnectionError()
        _assert_request_model(invocation.request_body, self.provider_name, self._model_id)
        wall_seconds = invocation.timeout_ms / 1000
        timeout = httpx.Timeout(
            wall_seconds,
            connect=min(wall_seconds, self._limits.connect_timeout_seconds),
            read=min(wall_seconds, self._limits.read_timeout_seconds),
            write=wall_seconds,
            pool=wall_seconds,
        )
        try:
            with self._client.stream(
                "POST",
                self._endpoint,
                headers=self._headers(self._api_key.get_secret_value()),
                content=invocation.request_body,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                body = _read_bounded(response, self._limits.max_response_bytes)
                if not 200 <= response.status_code < 300:
                    raise _http_error(response, body)
                return body
        except httpx.TimeoutException:
            raise ProviderTimeoutError() from None
        except ProviderTransportError:
            raise
        except httpx.HTTPError:
            raise ProviderConnectionError() from None

    def _build_endpoint(self, model_id: str) -> str:
        raise NotImplementedError

    def _headers(self, api_key: str) -> Mapping[str, str]:
        raise NotImplementedError


@final
class OpenAIProviderTransport(_PinnedHTTPTransport):
    provider_name = ProviderName.OPENAI
    allowed_host = "api.openai.com"

    def _build_endpoint(self, model_id: str) -> str:
        return OPENAI_ENDPOINT

    def _headers(self, api_key: str) -> Mapping[str, str]:
        return {
            "accept": "application/json",
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }


@final
class AnthropicProviderTransport(_PinnedHTTPTransport):
    provider_name = ProviderName.ANTHROPIC
    allowed_host = "api.anthropic.com"

    def _build_endpoint(self, model_id: str) -> str:
        return ANTHROPIC_ENDPOINT

    def _headers(self, api_key: str) -> Mapping[str, str]:
        return {
            "accept": "application/json",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": api_key,
        }


@final
class GoogleProviderTransport(_PinnedHTTPTransport):
    provider_name = ProviderName.GOOGLE
    allowed_host = "generativelanguage.googleapis.com"

    def _build_endpoint(self, model_id: str) -> str:
        path_model = model_id.removeprefix("models/")
        if not path_model:
            raise ValueError("Google model ID must not be blank")
        return f"{GOOGLE_ENDPOINT_PREFIX}{quote(path_model, safe='')}:generateContent"

    def _headers(self, api_key: str) -> Mapping[str, str]:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "x-goog-api-key": api_key,
        }


LiveClientFactory = Callable[[ProviderName], httpx.Client]


def build_live_prediction_providers(
    plan: LiveProviderPlan,
    *,
    environ: Mapping[str, str],
    prediction_schema: Mapping[str, Any],
    client_factory: LiveClientFactory | None = None,
    limits: ProviderTransportLimits | None = None,
) -> tuple[PredictionProvider, ...]:
    """Build production adapters exclusively with the three pinned live transports."""

    from .adapters import (
        AnthropicPredictionProvider,
        GooglePredictionProvider,
        OpenAIPredictionProvider,
    )

    transport_types = {
        ProviderName.OPENAI: OpenAIProviderTransport,
        ProviderName.ANTHROPIC: AnthropicProviderTransport,
        ProviderName.GOOGLE: GoogleProviderTransport,
    }
    providers: list[PredictionProvider] = []
    for item in plan.providers:
        api_key = environ.get(item.api_key_env)
        if not api_key:
            raise ValueError(f"missing provider secret environment variable: {item.api_key_env}")
        client = client_factory(item.variant.provider) if client_factory is not None else None
        transport = transport_types[item.variant.provider](
            api_key=api_key,
            model_id=item.variant.requested_model_id,
            client=client,
            limits=limits,
        )
        if item.variant.provider is ProviderName.OPENAI:
            provider: PredictionProvider = OpenAIPredictionProvider(
                item.variant, transport, prediction_schema
            )
        elif item.variant.provider is ProviderName.ANTHROPIC:
            provider = AnthropicPredictionProvider(item.variant, transport, prediction_schema)
        else:
            provider = GooglePredictionProvider(item.variant, transport, prediction_schema)
        providers.append(provider)
    return tuple(providers)


def is_live_provider_transport(
    transport: ProviderTransport,
    provider: ProviderName,
) -> bool:
    expected = {
        ProviderName.OPENAI: OpenAIProviderTransport,
        ProviderName.ANTHROPIC: AnthropicProviderTransport,
        ProviderName.GOOGLE: GoogleProviderTransport,
    }[provider]
    return type(transport) is expected


def _validate_model_id(model_id: str) -> str:
    if not model_id or model_id != model_id.strip() or len(model_id) > 256:
        raise ValueError("provider model ID is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in model_id):
        raise ValueError("provider model ID is invalid")
    return model_id


def _assert_pinned_endpoint(endpoint: str, allowed_host: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != allowed_host
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("provider endpoint is not pinned to the approved HTTPS host")


def _assert_request_model(body: bytes, provider: ProviderName, expected_model: str) -> None:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderConnectionError() from None
    if not isinstance(document, dict):
        raise ProviderConnectionError()
    if provider is ProviderName.GOOGLE:
        if "model" in document:
            raise ProviderConnectionError()
        return
    if document.get("model") != expected_model:
        raise ProviderConnectionError()


def _read_bounded(response: httpx.Response, limit_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > limit_bytes:
                raise ProviderResponseTooLargeError(limit_bytes=limit_bytes)
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit_bytes:
            raise ProviderResponseTooLargeError(limit_bytes=limit_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def _http_error(response: httpx.Response, body: bytes) -> ProviderHTTPError:
    return ProviderHTTPError(
        status_code=response.status_code,
        error_code=_extract_error_code(body),
        request_id=_safe_metadata(
            response.headers.get("x-request-id") or response.headers.get("request-id")
        ),
        retry_after_seconds=_parse_retry_after(response.headers.get("retry-after")),
    )


def _extract_error_code(body: bytes) -> str | None:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    error = document.get("error")
    if not isinstance(error, dict):
        return None
    for key in ("code", "type", "status"):
        value = error.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, str):
            safe = _safe_metadata(value)
            if safe is not None:
                return safe
    return None


def _safe_metadata(value: str | None) -> str | None:
    return value if value is not None and _SAFE_VALUE.fullmatch(value) else None


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    return max(0.0, seconds)
