import {
  isRecord,
  isRevisionMetadata,
  isStringArray,
} from "../apiValidation";
import { OperatorApiError } from "../matches/api";
import type { ApiErrorEnvelope } from "../matches/types";
import type {
  PerformanceApiClient,
  PerformanceQuery,
  PerformanceResponse,
} from "./types";

type ClientOptions = {
  token: string;
  baseUrl?: string;
};

function isErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ApiErrorEnvelope>;
  return (
    typeof candidate.code === "string" &&
    typeof candidate.message === "string" &&
    typeof candidate.retryable === "boolean" &&
    typeof candidate.correlation_id === "string"
  );
}

function isPerformanceResponse(value: unknown): value is PerformanceResponse {
  if (!isRecord(value) || !isRevisionMetadata(value.metadata) || !isRecord(value.data)) {
    return false;
  }
  return (
    typeof value.data.cohort_policy_version === "string" &&
    Array.isArray(value.data.items) &&
    value.data.items.every(isPerformanceRow)
  );
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return isFiniteNumber(value) && Number.isInteger(value) && value >= 0;
}

function isPerformanceMetrics(value: unknown): boolean {
  return (
    isRecord(value) &&
    isNonNegativeInteger(value.winner_n) &&
    isNonNegativeInteger(value.set_n) &&
    isNullableNumber(value.winner_accuracy) &&
    isNullableNumber(value.brier) &&
    isNullableNumber(value.log_loss) &&
    isNullableNumber(value.set_rps) &&
    isNullableNumber(value.set_score_accuracy)
  );
}

function isCalibrationBin(value: unknown): boolean {
  return (
    isRecord(value) &&
    isFiniteNumber(value.lower_bound) &&
    isFiniteNumber(value.upper_bound) &&
    typeof value.includes_upper_bound === "boolean" &&
    isFiniteNumber(value.mean_probability) &&
    isFiniteNumber(value.observed_rate) &&
    isNonNegativeInteger(value.sample_size) &&
    isFiniteNumber(value.uncertainty_lower) &&
    isFiniteNumber(value.uncertainty_upper) &&
    value.interval_version === "fixed-width-0.2-wilson-95-v1"
  );
}

function isPairedMetric(value: unknown): boolean {
  return (
    isRecord(value) &&
    (value.metric === "brier" || value.metric === "log_loss") &&
    isNonNegativeInteger(value.sample_size) &&
    isNullableNumber(value.mean_difference) &&
    isNullableNumber(value.standard_error) &&
    isNullableNumber(value.confidence_lower) &&
    isNullableNumber(value.confidence_upper) &&
    value.version === "paired-common-match-normal-95-v1"
  );
}

function isPairedComparison(value: unknown): boolean {
  if (!isRecord(value) || !isRecord(value.excluded_reasons)) return false;
  return (
    ["home_rate", "statistical", "market"].includes(String(value.baseline_kind)) &&
    typeof value.baseline_model_version === "string" &&
    isNonNegativeInteger(value.ai_individual_n) &&
    isNonNegativeInteger(value.baseline_individual_n) &&
    isNonNegativeInteger(value.paired_n) &&
    Object.values(value.excluded_reasons).every(isNonNegativeInteger) &&
    isPairedMetric(value.brier) &&
    isPairedMetric(value.log_loss)
  );
}

function isPerformanceRow(value: unknown): boolean {
  if (!isRecord(value) || !isRecord(value.cohort)) return false;
  return (
    typeof value.provider === "string" &&
    typeof value.model_version === "string" &&
    typeof value.prompt_version === "string" &&
    typeof value.prediction_type === "string" &&
    (value.division === "men" || value.division === "women") &&
    typeof value.competition === "string" &&
    Object.values(value.cohort).every((item) => typeof item === "string") &&
    isNonNegativeInteger(value.sample_size) &&
    isNonNegativeInteger(value.failure_count) &&
    isNonNegativeInteger(value.excluded_count) &&
    isNonNegativeInteger(value.corrected_evaluation_count) &&
    isNonNegativeInteger(value.paired_sample_size) &&
    isPerformanceMetrics(value.metrics) &&
    Array.isArray(value.calibration) &&
    value.calibration.every(isCalibrationBin) &&
    Array.isArray(value.comparisons) &&
    value.comparisons.every(isPairedComparison) &&
    ["available", "missing", "not_supported", "unverified"].includes(
      String(value.market_availability),
    ) &&
    typeof value.evaluator_version === "string" &&
    typeof value.cohort_policy_version === "string" &&
    isStringArray(value.evaluation_revision_ids) &&
    isStringArray(value.result_revision_ids)
  );
}

function queryString(query: PerformanceQuery): string {
  const parameters = new URLSearchParams();
  const entries = Object.entries(query).filter(
    (entry): entry is [string, string] => typeof entry[1] === "string" && entry[1].length > 0,
  );
  for (const [name, value] of entries) parameters.set(name, value);
  const serialized = parameters.toString();
  return serialized ? `?${serialized}` : "";
}

export function createPerformanceApiClient({
  token,
  baseUrl = "",
}: ClientOptions): PerformanceApiClient {
  const normalizedBase = baseUrl.replace(/\/$/, "");

  return {
    dataMode: "live",
    async getPerformance(query, signal) {
      const response = await fetch(
        `${normalizedBase}/api/v1/performance${queryString(query)}`,
        {
          headers: { Authorization: `Bearer ${token}` },
          signal,
        },
      );
      const payload: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        if (isErrorEnvelope(payload)) throw new OperatorApiError(response.status, payload);
        throw new Error(`API request failed with status ${response.status}`);
      }
      if (!isPerformanceResponse(payload)) {
        throw new Error("API response does not match the performance envelope");
      }
      return payload;
    },
  };
}
