"""Public read models for the operator API.

The read contract deliberately contains normalized facts and safe failure codes only.
Raw source/provider payloads, URLs, prompts, and credentials never cross this boundary.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field

SchemaVersion = Literal["vlytics.operator.v1"]
Division = Literal["men", "women"]
Availability = Literal["available", "missing", "not_supported", "unverified"]
ProviderStatus = Literal["succeeded", "failed", "timed_out", "budget_skipped", "missing"]


class APIError(BaseModel):
    """Safe error envelope shared by validation, authorization, and service failures."""

    code: str
    message: str
    retryable: bool
    correlation_id: str


class RevisionMetadata(BaseModel):
    """Reproducibility identifiers attached to every protected response."""

    schema_version: SchemaVersion = "vlytics.operator.v1"
    source_snapshot_ids: list[str] = Field(default_factory=list)
    schedule_revision_ids: list[str] = Field(default_factory=list)
    prediction_revision_ids: list[str] = Field(default_factory=list)
    evaluation_revision_ids: list[str] = Field(default_factory=list)
    model_versions: dict[str, list[str]] = Field(default_factory=dict)


class TeamSummary(BaseModel):
    id: str
    code: str
    name: str


class MarketSummary(BaseModel):
    availability: Availability
    source: str | None = None
    snapshot_id: str | None = None
    quoted_at: datetime | None = None
    reason: str | None = None


class ProviderOutcome(BaseModel):
    provider: str
    status: ProviderStatus
    requested_model: str | None = None
    resolved_model_id: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    generated_at: datetime | None = None
    prediction_revision_id: str | None = None
    output: dict[str, object] | None = None
    error_code: str | None = None


class MatchSummary(BaseModel):
    id: str
    competition: str
    division: Division
    stage: str
    scheduled_start_at_utc: datetime
    scheduled_start_at_local: datetime
    timezone: Literal["UTC", "Asia/Seoul"]
    status: str
    home_team: TeamSummary
    away_team: TeamSummary
    market: MarketSummary
    provider_outcomes: list[ProviderOutcome]


class MatchDetail(MatchSummary):
    actual_start_at_utc: datetime | None = None
    venue: str | None = None
    result: dict[str, object] | None = None
    feature_snapshot_id: str | None = None
    feature_version: str | None = None
    source_coverage: dict[str, Availability] = Field(default_factory=dict)


class PredictionHistoryItem(BaseModel):
    id: str
    match_id: str
    competition: str
    division: Division
    provider: str
    prediction_type: str
    requested_model: str
    resolved_model_id: str
    model_version: str | None = None
    prompt_version: str
    feature_version: str
    schedule_revision_id: str
    source_snapshot_id: str
    generated_at: datetime
    status: str
    output: dict[str, object]
    evaluation_revision_id: str | None = None


class PerformanceMetrics(BaseModel):
    """Server-owned aggregate metrics; unavailable estimates are explicit nulls."""

    winner_n: int = Field(ge=0)
    set_n: int = Field(ge=0)
    winner_accuracy: float | None = None
    brier: float | None = None
    log_loss: float | None = None
    set_rps: float | None = None
    set_score_accuracy: float | None = None


class CalibrationBinSummary(BaseModel):
    lower_bound: float = Field(ge=0, le=1)
    upper_bound: float = Field(ge=0, le=1)
    includes_upper_bound: bool
    mean_probability: float = Field(ge=0, le=1)
    observed_rate: float = Field(ge=0, le=1)
    sample_size: int = Field(ge=1)
    uncertainty_lower: float = Field(ge=0, le=1)
    uncertainty_upper: float = Field(ge=0, le=1)
    interval_version: Literal["fixed-width-0.2-wilson-95-v1"] = "fixed-width-0.2-wilson-95-v1"


class PairedMetricSummary(BaseModel):
    metric: Literal["brier", "log_loss"]
    sample_size: int = Field(ge=0)
    mean_difference: float | None
    standard_error: float | None
    confidence_lower: float | None
    confidence_upper: float | None
    version: Literal["paired-common-match-normal-95-v1"] = "paired-common-match-normal-95-v1"


class PairedComparisonSummary(BaseModel):
    baseline_kind: Literal["home_rate", "statistical", "market"]
    baseline_model_version: str
    ai_individual_n: int = Field(ge=0)
    baseline_individual_n: int = Field(ge=0)
    paired_n: int = Field(ge=0)
    excluded_reasons: dict[str, int] = Field(default_factory=dict)
    brier: PairedMetricSummary
    log_loss: PairedMetricSummary


class PerformanceRow(BaseModel):
    provider: str
    model_version: str
    prompt_version: str
    prediction_type: str
    division: Division
    competition: str
    cohort: dict[str, str]
    sample_size: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    corrected_evaluation_count: int = Field(ge=0)
    paired_sample_size: int = Field(ge=0)
    metrics: PerformanceMetrics
    calibration: list[CalibrationBinSummary] = Field(default_factory=list)
    comparisons: list[PairedComparisonSummary] = Field(default_factory=list)
    market_availability: Availability
    evaluator_version: str
    cohort_policy_version: str
    evaluation_revision_ids: list[str] = Field(default_factory=list)
    result_revision_ids: list[str] = Field(default_factory=list)


class OperationSummary(BaseModel):
    id: str
    job_type: str
    state: str
    due_at: datetime
    deadline_at: datetime | None = None
    attempt_no: int
    error_code: str | None = None
    retryable: bool


class RetryJobData(BaseModel):
    job_id: str
    state: Literal["retry_wait"]
    due_at: datetime
    deadline_at: datetime | None = None
    idempotent_replay: bool


class CoverageSummary(BaseModel):
    data_kind: str
    availability: Availability
    count: int
    latest_observed_at: datetime
    evidence_codes: list[str]


class ScheduleData(BaseModel):
    date: date
    timezone: Literal["UTC", "Asia/Seoul"]
    items: list[MatchSummary]
    next_cursor: str | None = None


class PredictionHistoryData(BaseModel):
    items: list[PredictionHistoryItem]
    next_cursor: str | None = None


class PerformanceData(BaseModel):
    cohort_policy_version: str
    items: list[PerformanceRow]


class ProviderBudgetSummary(BaseModel):
    provider: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    period: Literal["day", "month"]
    period_start: date
    period_end: date
    as_of: datetime
    reserved_amount: Decimal = Field(ge=0)
    actual_amount: Decimal = Field(ge=0)
    effective_amount: Decimal = Field(ge=0)
    reservation_count: int = Field(ge=0)
    settled_count: int = Field(ge=0)
    outstanding_count: int = Field(ge=0)
    conservative_charge_count: int = Field(ge=0)


class OperationsData(BaseModel):
    items: list[OperationSummary]
    budgets: list[ProviderBudgetSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class CoverageData(BaseModel):
    items: list[CoverageSummary]


class ScheduleResponse(BaseModel):
    metadata: RevisionMetadata
    data: ScheduleData


class MatchDetailResponse(BaseModel):
    metadata: RevisionMetadata
    data: MatchDetail


class PredictionHistoryResponse(BaseModel):
    metadata: RevisionMetadata
    data: PredictionHistoryData


class PerformanceResponse(BaseModel):
    metadata: RevisionMetadata
    data: PerformanceData


class OperationsResponse(BaseModel):
    metadata: RevisionMetadata
    data: OperationsData


class CoverageResponse(BaseModel):
    metadata: RevisionMetadata
    data: CoverageData


class RetryJobResponse(BaseModel):
    metadata: RevisionMetadata
    data: RetryJobData


PageSize = Annotated[int, Field(ge=1, le=100)]
