"""Provider-specific request and response envelopes over the common contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .base import (
    SYSTEM_PROMPT,
    PredictionProvider,
    ai_output_schema_document,
    parse_json_body,
)
from .models import (
    ParsedProviderResponse,
    PredictionContextV1,
    ProviderName,
    ProviderResponseMetadata,
    ProviderUsage,
)


class OpenAIPredictionProvider(PredictionProvider):
    provider_name = ProviderName.OPENAI

    def build_request_document(self, context: PredictionContextV1) -> Mapping[str, Any]:
        return {
            "model": self.variant.requested_model_id,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": context.snapshot_bytes.decode("utf-8"),
                        }
                    ],
                },
            ],
            "max_output_tokens": self.variant.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "vlytics_ai_prediction_v1",
                    "strict": True,
                    "schema": ai_output_schema_document(),
                }
            },
        }

    def parse_response_metadata(self, body: bytes) -> ProviderResponseMetadata:
        document = parse_json_body(body)
        usage = _mapping(document["usage"], "OpenAI usage")
        return ProviderResponseMetadata(
            request_id=_string(document["id"], "OpenAI request ID"),
            resolved_model_id=_string(document["model"], "OpenAI model"),
            usage=ProviderUsage(
                input_tokens=_integer(usage["input_tokens"], "OpenAI input tokens"),
                output_tokens=_integer(usage["output_tokens"], "OpenAI output tokens"),
            ),
        )

    def parse_response(
        self,
        body: bytes,
        metadata: ProviderResponseMetadata,
    ) -> ParsedProviderResponse:
        document = parse_json_body(body)
        text_parts: list[str] = []
        refusal: str | None = None
        for raw_output in _sequence(document["output"], "OpenAI output"):
            output_item = _mapping(raw_output, "OpenAI output item")
            raw_content = output_item.get("content")
            if raw_content is None:
                continue
            for item in _sequence(raw_content, "OpenAI content"):
                part = _mapping(item, "OpenAI content")
                if part.get("type") == "output_text":
                    text_parts.append(_string(part["text"], "OpenAI output text"))
                elif part.get("type") == "refusal":
                    refusal = _string(part.get("refusal", "refused"), "OpenAI refusal")
        text = "".join(text_parts) or None
        return ParsedProviderResponse(
            request_id=metadata.request_id,
            resolved_model_id=metadata.resolved_model_id,
            output_text=text,
            usage=metadata.usage,
            refusal_reason=refusal,
        )


class AnthropicPredictionProvider(PredictionProvider):
    provider_name = ProviderName.ANTHROPIC

    def build_request_document(self, context: PredictionContextV1) -> Mapping[str, Any]:
        return {
            "model": self.variant.requested_model_id,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": context.snapshot_bytes.decode("utf-8"),
                        }
                    ],
                }
            ],
            "max_tokens": self.variant.max_output_tokens,
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": ai_output_schema_document(),
                }
            },
        }

    def parse_response_metadata(self, body: bytes) -> ProviderResponseMetadata:
        document = parse_json_body(body)
        usage = _mapping(document["usage"], "Anthropic usage")
        return ProviderResponseMetadata(
            request_id=_string(document["id"], "Anthropic request ID"),
            resolved_model_id=_string(document["model"], "Anthropic model"),
            usage=ProviderUsage(
                input_tokens=_integer(usage["input_tokens"], "Anthropic input tokens"),
                output_tokens=_integer(usage["output_tokens"], "Anthropic output tokens"),
            ),
        )

    def parse_response(
        self,
        body: bytes,
        metadata: ProviderResponseMetadata,
    ) -> ParsedProviderResponse:
        document = parse_json_body(body)
        text_parts: list[str] = []
        refusal: str | None = None
        for item in _sequence(document["content"], "Anthropic content"):
            part = _mapping(item, "Anthropic content item")
            if part.get("type") == "text":
                text_parts.append(_string(part["text"], "Anthropic output text"))
            elif part.get("type") == "refusal":
                refusal = _string(part.get("refusal", "refused"), "Anthropic refusal")
        text = "".join(text_parts) or None
        if document.get("stop_reason") == "refusal" and refusal is None:
            refusal = "provider refusal"
        return ParsedProviderResponse(
            request_id=metadata.request_id,
            resolved_model_id=metadata.resolved_model_id,
            output_text=text,
            usage=metadata.usage,
            refusal_reason=refusal,
        )


class GooglePredictionProvider(PredictionProvider):
    provider_name = ProviderName.GOOGLE

    def build_request_document(self, context: PredictionContextV1) -> Mapping[str, Any]:
        return {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": context.snapshot_bytes.decode("utf-8")}],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": self.variant.max_output_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": ai_output_schema_document(),
            },
        }

    def parse_response_metadata(self, body: bytes) -> ProviderResponseMetadata:
        document = parse_json_body(body)
        usage = _mapping(document["usageMetadata"], "Google usage")
        return ProviderResponseMetadata(
            request_id=_string(document["responseId"], "Google response ID"),
            resolved_model_id=_string(document["modelVersion"], "Google model"),
            usage=ProviderUsage(
                input_tokens=_integer(
                    usage["promptTokenCount"],
                    "Google input tokens",
                ),
                output_tokens=_integer(
                    usage["candidatesTokenCount"],
                    "Google output tokens",
                ),
            ),
        )

    def parse_response(
        self,
        body: bytes,
        metadata: ProviderResponseMetadata,
    ) -> ParsedProviderResponse:
        document = parse_json_body(body)
        prompt_feedback = document.get("promptFeedback")
        if isinstance(prompt_feedback, Mapping) and prompt_feedback.get("blockReason"):
            return ParsedProviderResponse(
                request_id=metadata.request_id,
                resolved_model_id=metadata.resolved_model_id,
                output_text=None,
                usage=metadata.usage,
                refusal_reason="provider blocked the prompt",
            )
        candidates = _sequence(document["candidates"], "Google candidates")
        candidate = _mapping(candidates[0], "Google candidate")
        finish_reason = candidate.get("finishReason")
        if finish_reason in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "RECITATION"}:
            return ParsedProviderResponse(
                request_id=metadata.request_id,
                resolved_model_id=metadata.resolved_model_id,
                output_text=None,
                usage=metadata.usage,
                refusal_reason="provider blocked the candidate",
            )
        content = _mapping(candidate["content"], "Google content")
        parts = _sequence(content["parts"], "Google parts")
        text_parts = [
            _string(part["text"], "Google output text")
            for raw_part in parts
            if "text" in (part := _mapping(raw_part, "Google part"))
        ]
        text = "".join(text_parts) or None
        return ParsedProviderResponse(
            request_id=metadata.request_id,
            resolved_model_id=metadata.resolved_model_id,
            output_text=text,
            usage=metadata.usage,
            refusal_reason=None,
        )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes) or not value:
        raise TypeError(f"{name} must be a non-empty array")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{name} must be a non-negative integer")
    return int(value)
