"""Provider-neutral contracts for independent AI prediction attempts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from threading import Lock
from types import MappingProxyType
from typing import Any, Protocol

from vlytics.engine.features.definitions import (
    FEATURE_VERSION,
    MATCHUP_FEATURE_KEYS,
    PLAYER_FEATURE_KEYS,
    TEAM_FEATURE_KEYS,
)
from vlytics.engine.features.models import (
    FeatureSnapshot,
    FeatureValue,
    PlayerFeatures,
    compute_snapshot_sha256,
)


class ProviderName(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"


class VersionPolicy(StrEnum):
    UNCONFIGURED = "unconfigured"
    IMMUTABLE_MODEL_ID = "immutable_model_id"
    VERIFY_RESOLVED_MODEL_ID = "verify_resolved_model_id"


class AttemptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class FailureCode(StrEnum):
    CONFIG_UNRESOLVED = "config_unresolved"
    INPUT_POLICY_VIOLATION = "input_policy_violation"
    INPUT_TOO_LARGE = "input_too_large"
    BUDGET_SKIPPED = "budget_skipped"
    REFUSAL = "refusal"
    TIMEOUT = "timeout"
    INVALID_JSON = "invalid_json"
    NON_FINITE_PROBABILITY = "non_finite_probability"
    INVALID_PROBABILITY_SUM = "invalid_probability_sum"
    INVALID_OUTPUT = "invalid_output"
    ALIAS_DRIFT = "alias_drift"
    VERSION_UNVERIFIED = "version_unverified"
    PROVIDER_ERROR = "provider_error"


_FORBIDDEN_INPUT_PARTS = {
    "market",
    "markets",
    "odds",
    "sportsbook",
    "bookmaker",
    "search",
    "tools",
    "tool",
}


def canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_snapshot_policy(document: Mapping[str, Any]) -> None:
    """Reject Market, search/tool, and other-model inputs at the provider boundary."""

    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for raw_key, nested in value.items():
                key = str(raw_key).lower()
                parts = set(key.replace("-", "_").split("_"))
                is_other_prediction = "prediction" in parts and bool(
                    parts & {"other", "provider", "model"}
                )
                if parts & _FORBIDDEN_INPUT_PARTS or is_other_prediction:
                    raise ValueError(f"forbidden provider input at {path}.{raw_key}")
                visit(nested, f"{path}.{raw_key}")
        elif isinstance(value, list | tuple):
            for index, nested in enumerate(value):
                visit(nested, f"{path}[{index}]")

    visit(document, "$")


@dataclass(frozen=True, init=False)
class PredictionContextV1:
    """One verified immutable feature snapshot shared byte-for-byte by all providers."""

    snapshot_id: str
    snapshot_sha256: str
    snapshot_document: Mapping[str, Any] = field(repr=False)
    snapshot_bytes: bytes = field(repr=False)

    @classmethod
    def from_feature_snapshot(
        cls,
        snapshot_id: str,
        snapshot: FeatureSnapshot,
    ) -> PredictionContextV1:
        _validate_feature_snapshot_shape(snapshot)
        computed = compute_snapshot_sha256(snapshot)
        if computed != snapshot.sha256:
            raise ValueError("feature snapshot digest is not valid")
        if not snapshot_id.strip():
            raise ValueError("snapshot_id must not be blank")
        document = snapshot.to_dict()
        validate_snapshot_policy(document)
        context = object.__new__(cls)
        object.__setattr__(context, "snapshot_id", snapshot_id)
        object.__setattr__(context, "snapshot_sha256", snapshot.sha256)
        object.__setattr__(context, "snapshot_document", MappingProxyType(document))
        object.__setattr__(context, "snapshot_bytes", canonical_json_bytes(document))
        return context


@dataclass(frozen=True)
class ProviderVariant:
    variant_id: str
    provider: ProviderName
    requested_model_id: str
    pinned_model_version: str
    version_policy: VersionPolicy
    prompt_version: str
    prompt_hash: str
    distribution_version: str
    max_input_tokens: int
    max_output_tokens: int
    input_cost_per_million: Decimal
    output_cost_per_million: Decimal
    enabled: bool
    op003_resolved: bool

    def __post_init__(self) -> None:
        text_fields = (
            self.variant_id,
            self.requested_model_id,
            self.pinned_model_version,
            self.prompt_version,
            self.prompt_hash,
            self.distribution_version,
        )
        if any(not value.strip() for value in text_fields):
            raise ValueError("provider variant string fields must not be blank")
        if len(self.prompt_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.prompt_hash
        ):
            raise ValueError("provider prompt_hash must be a lowercase SHA-256 digest")
        if self.max_input_tokens < 0 or self.max_output_tokens < 0:
            raise ValueError("provider token limits must not be negative")
        if self.operational and (self.max_input_tokens == 0 or self.max_output_tokens == 0):
            raise ValueError("operational provider token limits must be positive")
        if self.input_cost_per_million < 0 or self.output_cost_per_million < 0:
            raise ValueError("provider prices must not be negative")
        if self.operational and self.version_policy is VersionPolicy.UNCONFIGURED:
            raise ValueError("operational provider version_policy must be configured")
        if (
            self.operational
            and self.version_policy is VersionPolicy.IMMUTABLE_MODEL_ID
            and self.requested_model_id != self.pinned_model_version
        ):
            raise ValueError("immutable model policy requires the requested ID to be pinned")

    @property
    def operational(self) -> bool:
        unresolved = "__UNRESOLVED" in (self.requested_model_id + self.pinned_model_version)
        return self.enabled and self.op003_resolved and not unresolved

    @property
    def maximum_call_cost(self) -> Decimal:
        million = Decimal(1_000_000)
        return (
            Decimal(self.max_input_tokens) * self.input_cost_per_million
            + Decimal(self.max_output_tokens) * self.output_cost_per_million
        ) / million


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token usage must not be negative")


class BudgetAccount(Protocol):
    """Reservation boundary shared by local tests and durable PostgreSQL accounting."""

    def reserve(self, amount: Decimal) -> bool: ...

    def settle(self, reserved: Decimal, actual: Decimal) -> None: ...

    def release(self, reserved: Decimal) -> None: ...


class BudgetLedger:
    """A process-local budget account for isolated tests and dry runs."""

    def __init__(self, cap: Decimal, *, spent: Decimal = Decimal("0")) -> None:
        if cap < 0 or spent < 0 or spent > cap:
            raise ValueError("budget cap and spent amount are inconsistent")
        self.cap = cap
        self._spent = spent
        self._reserved = Decimal("0")
        self._lock = Lock()

    @property
    def spent(self) -> Decimal:
        with self._lock:
            return self._spent

    def reserve(self, amount: Decimal) -> bool:
        if amount < 0:
            raise ValueError("budget reservation must not be negative")
        with self._lock:
            if self._spent + self._reserved + amount > self.cap:
                return False
            self._reserved += amount
            return True

    def settle(self, reserved: Decimal, actual: Decimal) -> None:
        if reserved < 0 or actual < 0:
            raise ValueError("provider costs must not be negative")
        with self._lock:
            if reserved > self._reserved:
                raise ValueError("provider cost reservation was not found")
            self._reserved -= reserved
            self._spent += actual

    def release(self, reserved: Decimal) -> None:
        if reserved < 0:
            raise ValueError("budget release must not be negative")
        with self._lock:
            if reserved > self._reserved:
                raise ValueError("provider cost reservation was not found")
            self._reserved -= reserved


@dataclass(frozen=True)
class ProviderInvocation:
    provider: ProviderName
    variant_id: str
    snapshot_sha256: str
    snapshot_bytes: bytes
    request_body: bytes
    estimated_input_tokens: int
    timeout_ms: int


@dataclass(frozen=True)
class ParsedProviderResponse:
    request_id: str
    resolved_model_id: str
    output_text: str | None
    usage: ProviderUsage
    refusal_reason: str | None = None


@dataclass(frozen=True)
class ProviderResponseMetadata:
    request_id: str
    resolved_model_id: str
    usage: ProviderUsage


@dataclass(frozen=True)
class PredictionAttemptResult:
    provider: ProviderName
    variant_id: str
    status: AttemptStatus
    failure_code: FailureCode | None
    snapshot_sha256: str
    requested_model_id: str
    pinned_model_version: str
    version_policy: VersionPolicy
    prompt_version: str
    prompt_hash: str
    request_hash: str
    response_hash: str | None
    resolved_model_id: str | None
    request_id: str | None
    usage: ProviderUsage | None
    cost: Decimal
    latency_ms: int
    output: Mapping[str, Any] | None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.latency_ms < 0 or self.cost < 0:
            raise ValueError("attempt latency and cost must not be negative")
        if self.status is AttemptStatus.SUCCEEDED:
            if self.failure_code is not None or self.output is None:
                raise ValueError("successful attempts require output and no failure")
        elif self.failure_code is None or self.output is not None:
            raise ValueError("unsuccessful attempts require a failure and no output")


def _validate_feature_snapshot_shape(snapshot: FeatureSnapshot) -> None:
    if type(snapshot) is not FeatureSnapshot:
        raise TypeError("provider input must be an exact FeatureSnapshot")
    if snapshot.feature_version != FEATURE_VERSION:
        raise ValueError("provider input must use feature-v1")
    if set(snapshot.team_features) != {"home", "away"}:
        raise ValueError("feature snapshot must contain exactly home and away team features")
    expected_team = set(TEAM_FEATURE_KEYS)
    if any(set(features) != expected_team for features in snapshot.team_features.values()):
        raise ValueError("team feature keys do not match the feature-v1 allowlist")
    for features in snapshot.team_features.values():
        _validate_feature_values(features)
    if set(snapshot.matchup_features) != set(MATCHUP_FEATURE_KEYS):
        raise ValueError("matchup feature keys do not match the feature-v1 allowlist")
    _validate_feature_values(snapshot.matchup_features)
    expected_players = set(PLAYER_FEATURE_KEYS)
    for player in snapshot.players.values():
        if type(player) is not PlayerFeatures:
            raise TypeError("provider player input must use typed PlayerFeatures")
        if player.side not in {"home", "away"}:
            raise ValueError("player feature side must be home or away")
        if not player.team_id.strip() or set(player.features) != expected_players:
            raise ValueError("player features do not match the feature-v1 allowlist")
        _validate_feature_values(player.features)
    document = snapshot.to_dict()
    if set(document) != {
        "feature_version",
        "availability_policy",
        "target_match_id",
        "schedule_revision_id",
        "cutoff_at",
        "captured_at",
        "lineup_status",
        "team_features",
        "matchup_features",
        "players",
        "lineage",
        "sha256",
    }:
        raise ValueError("feature snapshot fields do not match feature-v1")


def _validate_feature_values(features: Mapping[str, Any]) -> None:
    for key, value in features.items():
        if type(value) is not FeatureValue:
            raise TypeError("provider feature input must use typed FeatureValue")
        if value.definition != key:
            raise ValueError("feature definition does not match its allowlisted key")
