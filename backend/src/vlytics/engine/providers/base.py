"""Common provider execution boundary with validation and immutable evidence."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from decimal import Decimal
from queue import Empty, Queue
from threading import Thread
from time import perf_counter
from typing import Any, Protocol

from vlytics.engine.prediction_validation import (
    PredictionContractError,
    PredictionValidationContext,
    validate_prediction_output_v1,
)

from .models import (
    AttemptStatus,
    BudgetAccount,
    FailureCode,
    ParsedProviderResponse,
    PredictionAttemptResult,
    PredictionContextV1,
    ProviderInvocation,
    ProviderName,
    ProviderResponseMetadata,
    ProviderUsage,
    ProviderVariant,
    VersionPolicy,
    canonical_json_bytes,
    sha256_bytes,
)

SYSTEM_PROMPT = (
    "Predict the volleyball match only from the supplied frozen feature snapshot. "
    "Do not search, call tools, use market data, odds, or another model prediction. "
    "Return only JSON matching the supplied schema. Probabilities must be finite and "
    "the six set-score probabilities must sum to exactly one within 1e-6."
)
USER_PROMPT_TEMPLATE = "{{FEATURE_SNAPSHOT_JSON}}"

AI_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "home_win_probability",
        "set_score_probabilities",
        "rationale",
        "risk_factors",
    ],
    "properties": {
        "home_win_probability": {"type": "number", "minimum": 0, "maximum": 1},
        "set_score_probabilities": {
            "type": "array",
            "minItems": 6,
            "maxItems": 6,
            "prefixItems": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["outcome", "probability"],
                    "properties": {
                        "outcome": {"const": outcome},
                        "probability": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                }
                for outcome in ("3:0", "3:1", "3:2", "2:3", "1:3", "0:3")
            ],
            "items": False,
        },
        "rationale": {"type": "string", "minLength": 1},
        "risk_factors": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "confidence_note": {"type": "string", "minLength": 1},
    },
}
_AI_OUTPUT_SCHEMA_BYTES = canonical_json_bytes(AI_OUTPUT_SCHEMA)


def ai_output_schema_document() -> dict[str, Any]:
    """Return the immutable canonical schema as a fresh request-owned object."""

    value = json.loads(_AI_OUTPUT_SCHEMA_BYTES)
    assert isinstance(value, dict)
    return value


PROMPT_TEMPLATE_HASH = sha256_bytes(
    canonical_json_bytes(
        {
            "system": SYSTEM_PROMPT,
            "user": USER_PROMPT_TEMPLATE,
            "output_schema": ai_output_schema_document(),
        }
    )
)


class ProviderTimeoutError(TimeoutError):
    """A provider request timed out after dispatch."""

    def __init__(self, *, usage: ProviderUsage | None = None) -> None:
        super().__init__("provider request timed out")
        self.usage = usage


class NonFiniteJsonError(ValueError):
    """JSON used NaN or Infinity, which RFC 8259 does not permit."""


class ProviderTransport(Protocol):
    """Injected I/O boundary implemented by reviewed live or deterministic test transports."""

    def send(self, invocation: ProviderInvocation) -> bytes:
        """Return the exact provider response body or raise a transport exception."""


class PredictionProvider(ABC):
    """Provider-neutral prediction interface."""

    provider_name: ProviderName

    def __init__(
        self,
        variant: ProviderVariant,
        transport: ProviderTransport,
        prediction_schema: Mapping[str, Any],
    ) -> None:
        if variant.provider is not self.provider_name:
            raise ValueError("variant provider does not match the adapter")
        self.variant = variant
        self.transport = transport
        self.prediction_schema = prediction_schema

    def predict(
        self,
        context: PredictionContextV1,
        *,
        timeout_ms: int,
        budget: BudgetAccount,
    ) -> PredictionAttemptResult:
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        request_body, request_hash, preflight_failure = self._preflight_request(context)
        estimated_input_tokens = len(request_body)
        if preflight_failure is not None:
            return self._failure(
                context,
                preflight_failure,
                request_hash=request_hash,
                status=AttemptStatus.SKIPPED,
                detail="provider request violates the input policy",
            )

        if (
            not self.variant.operational
            or self.variant.prompt_hash != PROMPT_TEMPLATE_HASH
            or not _is_approved_transport(self.transport, self.provider_name)
        ):
            return self._failure(
                context,
                FailureCode.CONFIG_UNRESOLVED,
                request_hash=request_hash,
                status=AttemptStatus.SKIPPED,
                detail="provider configuration is unresolved",
            )
        if estimated_input_tokens > self.variant.max_input_tokens:
            return self._failure(
                context,
                FailureCode.INPUT_TOO_LARGE,
                request_hash=request_hash,
                status=AttemptStatus.SKIPPED,
                detail="provider input exceeds the configured token bound",
            )

        reserved = self.variant.maximum_call_cost
        if not budget.reserve(reserved):
            return self._failure(
                context,
                FailureCode.BUDGET_SKIPPED,
                request_hash=request_hash,
                status=AttemptStatus.SKIPPED,
                detail="provider budget cap was reached",
            )

        invocation = ProviderInvocation(
            provider=self.provider_name,
            variant_id=self.variant.variant_id,
            snapshot_sha256=context.snapshot_sha256,
            snapshot_bytes=context.snapshot_bytes,
            request_body=request_body,
            estimated_input_tokens=estimated_input_tokens,
            timeout_ms=timeout_ms,
        )
        started = perf_counter()
        try:
            response_body = _send_with_wall_clock_timeout(self.transport, invocation)
        except ProviderTimeoutError as error:
            usage = error.usage
            cost = self._cost(usage) if usage is not None else reserved
            budget.settle(reserved, cost)
            return self._failure(
                context,
                FailureCode.TIMEOUT,
                request_hash=request_hash,
                usage=usage,
                cost=cost,
                latency_ms=_elapsed_ms(started),
                detail="provider request timed out",
            )
        except Exception:
            budget.settle(reserved, reserved)
            return self._failure(
                context,
                FailureCode.PROVIDER_ERROR,
                request_hash=request_hash,
                cost=reserved,
                latency_ms=_elapsed_ms(started),
                detail="provider transport failed",
            )

        latency_ms = _elapsed_ms(started)
        response_hash = sha256_bytes(response_body)
        try:
            metadata = self.parse_response_metadata(response_body)
        except NonFiniteJsonError:
            budget.settle(reserved, reserved)
            return self._failure(
                context,
                FailureCode.NON_FINITE_PROBABILITY,
                request_hash=request_hash,
                response_hash=response_hash,
                cost=reserved,
                latency_ms=latency_ms,
                detail="provider response contains a non-finite number",
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            budget.settle(reserved, reserved)
            return self._failure(
                context,
                FailureCode.INVALID_JSON,
                request_hash=request_hash,
                response_hash=response_hash,
                cost=reserved,
                latency_ms=latency_ms,
                detail="provider response envelope is invalid",
            )

        cost = self._cost(metadata.usage)
        budget.settle(reserved, cost)
        if (
            metadata.usage.input_tokens > self.variant.max_input_tokens
            or metadata.usage.output_tokens > self.variant.max_output_tokens
        ):
            return self._failure(
                context,
                FailureCode.PROVIDER_ERROR,
                request_hash=request_hash,
                response_hash=response_hash,
                resolved_model_id=metadata.resolved_model_id,
                request_id=metadata.request_id,
                usage=metadata.usage,
                cost=cost,
                latency_ms=latency_ms,
                detail="provider usage exceeds configured token limits",
            )

        version_failure = self._version_failure(metadata.resolved_model_id)
        if version_failure is not None:
            return self._failure(
                context,
                version_failure,
                request_hash=request_hash,
                response_hash=response_hash,
                resolved_model_id=metadata.resolved_model_id,
                request_id=metadata.request_id,
                usage=metadata.usage,
                cost=cost,
                latency_ms=latency_ms,
                detail=(
                    "provider model version could not be verified"
                    if version_failure is FailureCode.VERSION_UNVERIFIED
                    else "provider resolved an unexpected model version"
                ),
            )

        try:
            parsed = self.parse_response(response_body, metadata)
        except (KeyError, TypeError, ValueError):
            return self._failure(
                context,
                FailureCode.INVALID_JSON,
                request_hash=request_hash,
                response_hash=response_hash,
                resolved_model_id=metadata.resolved_model_id,
                request_id=metadata.request_id,
                usage=metadata.usage,
                cost=cost,
                latency_ms=latency_ms,
                detail="provider response envelope is invalid",
            )
        if parsed.refusal_reason is not None or parsed.output_text is None:
            return self._failure(
                context,
                FailureCode.REFUSAL,
                request_hash=request_hash,
                response_hash=response_hash,
                resolved_model_id=metadata.resolved_model_id,
                request_id=metadata.request_id,
                usage=metadata.usage,
                cost=cost,
                latency_ms=latency_ms,
                detail="provider refused the prediction",
            )

        try:
            core = _strict_json_object(parsed.output_text)
            output = self._build_prediction_output(core, context, metadata.resolved_model_id)
            validate_prediction_output_v1(
                output,
                self.prediction_schema,
                context=PredictionValidationContext(
                    producer_variant_id=self.variant.variant_id,
                    input_snapshot_id=context.snapshot_id,
                ),
            )
        except NonFiniteJsonError:
            return self._output_failure(
                context,
                FailureCode.NON_FINITE_PROBABILITY,
                request_hash,
                response_hash,
                metadata,
                cost,
                latency_ms,
                "provider output contains a non-finite probability",
            )
        except json.JSONDecodeError:
            return self._output_failure(
                context,
                FailureCode.INVALID_JSON,
                request_hash,
                response_hash,
                metadata,
                cost,
                latency_ms,
                "provider output is not valid JSON",
            )
        except PredictionContractError as error:
            failure = (
                FailureCode.INVALID_PROBABILITY_SUM
                if "probability mass must sum to one" in str(error)
                else FailureCode.NON_FINITE_PROBABILITY
                if "finite" in str(error)
                else FailureCode.INVALID_OUTPUT
            )
            return self._output_failure(
                context,
                failure,
                request_hash,
                response_hash,
                metadata,
                cost,
                latency_ms,
                "provider output violates the prediction contract",
            )
        except (KeyError, TypeError, ValueError):
            return self._output_failure(
                context,
                FailureCode.INVALID_OUTPUT,
                request_hash,
                response_hash,
                metadata,
                cost,
                latency_ms,
                "provider output violates the prediction contract",
            )

        return PredictionAttemptResult(
            provider=self.provider_name,
            variant_id=self.variant.variant_id,
            status=AttemptStatus.SUCCEEDED,
            failure_code=None,
            snapshot_sha256=context.snapshot_sha256,
            requested_model_id=self.variant.requested_model_id,
            pinned_model_version=self.variant.pinned_model_version,
            version_policy=self.variant.version_policy,
            prompt_version=self.variant.prompt_version,
            prompt_hash=self.variant.prompt_hash,
            request_hash=request_hash,
            response_hash=response_hash,
            resolved_model_id=metadata.resolved_model_id,
            request_id=metadata.request_id,
            usage=metadata.usage,
            cost=cost,
            latency_ms=latency_ms,
            output=output,
        )

    def preflight_request_hash(self, context: PredictionContextV1) -> str:
        """Return the deterministic provider-native request body hash without sending."""

        return self._preflight_request(context)[1]

    def _preflight_request(
        self,
        context: PredictionContextV1,
    ) -> tuple[bytes, str, FailureCode | None]:
        failure: FailureCode | None = None
        try:
            request_document = self.build_request_document(context)
            if _contains_forbidden_request_capability(request_document):
                failure = FailureCode.INPUT_POLICY_VIOLATION
            request_body = canonical_json_bytes(request_document)
        except Exception:
            failure = FailureCode.INPUT_POLICY_VIOLATION
            request_body = canonical_json_bytes(
                {
                    "provider": self.provider_name.value,
                    "snapshot_sha256": context.snapshot_sha256,
                    "variant_id": self.variant.variant_id,
                    "preflight": "request_construction_failed",
                }
            )
        return request_body, sha256_bytes(request_body), failure

    def _version_failure(self, resolved_model_id: str) -> FailureCode | None:
        if self.variant.version_policy is VersionPolicy.IMMUTABLE_MODEL_ID:
            return (
                None
                if resolved_model_id == self.variant.requested_model_id
                else FailureCode.ALIAS_DRIFT
            )
        if resolved_model_id == self.variant.requested_model_id:
            return FailureCode.VERSION_UNVERIFIED
        if resolved_model_id != self.variant.pinned_model_version:
            return FailureCode.ALIAS_DRIFT
        return None

    @abstractmethod
    def build_request_document(self, context: PredictionContextV1) -> Mapping[str, Any]:
        """Build a provider-native body without tools, search, or Market inputs."""

    @abstractmethod
    def parse_response_metadata(self, body: bytes) -> ProviderResponseMetadata:
        """Extract billing and model identity before output parsing."""

    @abstractmethod
    def parse_response(
        self,
        body: bytes,
        metadata: ProviderResponseMetadata,
    ) -> ParsedProviderResponse:
        """Extract refusal and structured output after billing is recorded."""

    def _build_prediction_output(
        self,
        core: Mapping[str, Any],
        context: PredictionContextV1,
        resolved_model_id: str,
    ) -> dict[str, Any]:
        allowed = {
            "home_win_probability",
            "set_score_probabilities",
            "rationale",
            "risk_factors",
            "confidence_note",
        }
        if set(core) - allowed:
            raise ValueError("structured output contains unsupported fields")
        required = allowed - {"confidence_note"}
        if required - set(core):
            raise ValueError("structured output is missing required fields")
        provenance = {
            "producer_variant_id": self.variant.variant_id,
            "distribution_version": self.variant.distribution_version,
            "probability_source": "ai_direct",
            "derivation_method": "ai_direct_structured_output",
            "input_snapshot_id": context.snapshot_id,
            "prompt_version": self.variant.prompt_version,
            "prompt_hash": self.variant.prompt_hash,
        }
        output: dict[str, Any] = {
            "schema_version": "prediction-v1",
            "producer_variant_id": self.variant.variant_id,
            "model_version": resolved_model_id,
            "distribution_version": self.variant.distribution_version,
            "capabilities": ["winner", "set_score"],
            "home_win_probability": core["home_win_probability"],
            "set_score_probabilities": core["set_score_probabilities"],
            "target_provenance": {
                "winner": dict(provenance),
                "set_score": dict(provenance),
            },
            "rationale": core["rationale"],
            "risk_factors": core["risk_factors"],
        }
        if "confidence_note" in core:
            output["confidence_note"] = core["confidence_note"]
        return output

    def _cost(self, usage: ProviderUsage) -> Decimal:
        million = Decimal(1_000_000)
        return (
            Decimal(usage.input_tokens) * self.variant.input_cost_per_million
            + Decimal(usage.output_tokens) * self.variant.output_cost_per_million
        ) / million

    def _output_failure(
        self,
        context: PredictionContextV1,
        code: FailureCode,
        request_hash: str,
        response_hash: str,
        metadata: ProviderResponseMetadata,
        cost: Decimal,
        latency_ms: int,
        detail: str,
    ) -> PredictionAttemptResult:
        return self._failure(
            context,
            code,
            request_hash=request_hash,
            response_hash=response_hash,
            resolved_model_id=metadata.resolved_model_id,
            request_id=metadata.request_id,
            usage=metadata.usage,
            cost=cost,
            latency_ms=latency_ms,
            detail=detail,
        )

    def _failure(
        self,
        context: PredictionContextV1,
        code: FailureCode,
        *,
        request_hash: str,
        status: AttemptStatus = AttemptStatus.FAILED,
        response_hash: str | None = None,
        resolved_model_id: str | None = None,
        request_id: str | None = None,
        usage: ProviderUsage | None = None,
        cost: Decimal = Decimal("0"),
        latency_ms: int = 0,
        detail: str | None = None,
    ) -> PredictionAttemptResult:
        return PredictionAttemptResult(
            provider=self.provider_name,
            variant_id=self.variant.variant_id,
            status=status,
            failure_code=code,
            snapshot_sha256=context.snapshot_sha256,
            requested_model_id=self.variant.requested_model_id,
            pinned_model_version=self.variant.pinned_model_version,
            version_policy=self.variant.version_policy,
            prompt_version=self.variant.prompt_version,
            prompt_hash=self.variant.prompt_hash,
            request_hash=request_hash,
            response_hash=response_hash,
            resolved_model_id=resolved_model_id,
            request_id=request_id,
            usage=usage,
            cost=cost,
            latency_ms=latency_ms,
            output=None,
            detail=detail,
        )


def parse_json_body(body: bytes) -> Mapping[str, Any]:
    value = json.loads(body.decode("utf-8"), parse_constant=_reject_non_finite)
    if not isinstance(value, Mapping):
        raise ValueError("provider response must be a JSON object")
    return value


def _strict_json_object(value: str) -> Mapping[str, Any]:
    document = json.loads(value, parse_constant=_reject_non_finite)
    if not isinstance(document, Mapping):
        raise ValueError("structured output must be a JSON object")
    return document


def _reject_non_finite(value: str) -> None:
    raise NonFiniteJsonError(f"non-finite JSON number: {value}")


def _send_with_wall_clock_timeout(
    transport: ProviderTransport,
    invocation: ProviderInvocation,
) -> bytes:
    queue: Queue[tuple[bool, bytes | Exception]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            queue.put((True, transport.send(invocation)))
        except Exception as error:
            queue.put((False, error))

    Thread(target=invoke, daemon=True, name=f"provider-{invocation.provider.value}").start()
    try:
        succeeded, value = queue.get(timeout=invocation.timeout_ms / 1000)
    except Empty as error:
        raise ProviderTimeoutError() from error
    if not succeeded:
        assert isinstance(value, Exception)
        raise value
    assert isinstance(value, bytes)
    return value


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _contains_forbidden_request_capability(document: Mapping[str, Any]) -> bool:
    forbidden = {"tools", "tool_choice", "web_search", "google_search", "grounding"}
    return any(str(key).lower() in forbidden for key in document)


def _is_approved_transport(transport: ProviderTransport, provider: ProviderName) -> bool:
    from .fake import FakeProviderTransport
    from .transports import is_live_provider_transport

    return type(transport) is FakeProviderTransport or is_live_provider_transport(
        transport,
        provider,
    )
