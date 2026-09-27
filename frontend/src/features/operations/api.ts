import { isIsoTimestamp, isNullableString, isRecord, isRevisionMetadata, isStringArray } from "../apiValidation";
import { OperatorApiError } from "../matches/api";
import type { ApiErrorEnvelope } from "../matches/types";
import type { CoverageResponse, OperationsApiClient, OperationsResponse, RetryJobResponse } from "./types";

function isErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (!value || typeof value !== "object") return false;
  const error = value as Partial<ApiErrorEnvelope>;
  return typeof error.code === "string" && typeof error.message === "string" && typeof error.retryable === "boolean" && typeof error.correlation_id === "string";
}

function hasEnvelope(value: unknown): value is { metadata: unknown; data: Record<string, unknown> } {
  return isRecord(value) && isRevisionMetadata(value.metadata) && isRecord(value.data);
}

function isOperation(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === "string" &&
    typeof value.job_type === "string" &&
    typeof value.state === "string" &&
    isIsoTimestamp(value.due_at) &&
    (value.deadline_at === null || isIsoTimestamp(value.deadline_at)) &&
    typeof value.attempt_no === "number" &&
    Number.isInteger(value.attempt_no) &&
    isNullableString(value.error_code) &&
    typeof value.retryable === "boolean"
  );
}

function isBudget(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.provider === "string" &&
    typeof value.currency === "string" &&
    (value.period === "day" || value.period === "month") &&
    typeof value.period_start === "string" &&
    typeof value.period_end === "string" &&
    isIsoTimestamp(value.as_of) &&
    typeof value.reserved_amount === "string" &&
    typeof value.actual_amount === "string" &&
    typeof value.effective_amount === "string" &&
    typeof value.reservation_count === "number" &&
    typeof value.settled_count === "number" &&
    typeof value.outstanding_count === "number" &&
    typeof value.conservative_charge_count === "number"
  );
}

function isCoverage(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.data_kind === "string" &&
    ["available", "missing", "not_supported", "unverified"].includes(String(value.availability)) &&
    typeof value.count === "number" &&
    isIsoTimestamp(value.latest_observed_at) &&
    isStringArray(value.evidence_codes)
  );
}

function isOperationsResponse(value: unknown): value is OperationsResponse {
  return hasEnvelope(value) &&
    Array.isArray(value.data.items) &&
    value.data.items.every(isOperation) &&
    Array.isArray(value.data.budgets) &&
    value.data.budgets.every(isBudget) &&
    isNullableString(value.data.next_cursor);
}

function isCoverageResponse(value: unknown): value is CoverageResponse {
  return hasEnvelope(value) && Array.isArray(value.data.items) && value.data.items.every(isCoverage);
}

function isRetryJobResponse(value: unknown): value is RetryJobResponse {
  if (!hasEnvelope(value)) return false;
  return (
    typeof value.data.job_id === "string" &&
    value.data.state === "retry_wait" &&
    isIsoTimestamp(value.data.due_at) &&
    (value.data.deadline_at === null || isIsoTimestamp(value.data.deadline_at)) &&
    typeof value.data.idempotent_replay === "boolean"
  );
}

export function createOperationsApiClient({ token, baseUrl = "" }: { token: string; baseUrl?: string }): OperationsApiClient {
  const base = baseUrl.replace(/\/$/, "");

  async function request<T>(
    path: string,
    validate: (value: unknown) => value is T,
    init: RequestInit = {},
  ): Promise<T> {
    const response = await fetch(`${base}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${token}`, ...init.headers },
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      if (isErrorEnvelope(payload)) throw new OperatorApiError(response.status, payload);
      throw new Error(`API request failed with status ${response.status}`);
    }
    if (!validate(payload)) throw new Error("API response does not match the operations envelope");
    return payload;
  }

  return {
    getOperations(query, signal) {
      const parameters = new URLSearchParams({ limit: String(query.limit ?? 25) });
      if (query.state) parameters.set("state", query.state);
      if (query.jobType) parameters.set("job_type", query.jobType);
      if (query.cursor) parameters.set("cursor", query.cursor);
      return request<OperationsResponse>(`/api/v1/operations?${parameters}`, isOperationsResponse, { signal });
    },
    getCoverage(signal) {
      return request<CoverageResponse>("/api/v1/operations/coverage", isCoverageResponse, { signal });
    },
    retryJob(jobId, idempotencyKey, signal) {
      return request<RetryJobResponse>(
        `/api/v1/jobs/${encodeURIComponent(jobId)}/retry`,
        isRetryJobResponse,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          signal,
        },
      );
    },
  };
}
