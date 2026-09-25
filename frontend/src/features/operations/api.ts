import { OperatorApiError } from "../matches/api";
import type { ApiErrorEnvelope } from "../matches/types";
import type { CoverageResponse, OperationsApiClient, OperationsResponse, RetryJobResponse } from "./types";

function isErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (!value || typeof value !== "object") return false;
  const error = value as Partial<ApiErrorEnvelope>;
  return typeof error.code === "string" && typeof error.message === "string" && typeof error.retryable === "boolean" && typeof error.correlation_id === "string";
}

export function createOperationsApiClient({ token, baseUrl = "" }: { token: string; baseUrl?: string }): OperationsApiClient {
  const base = baseUrl.replace(/\/$/, "");
  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${base}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${token}`, ...init.headers },
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      if (isErrorEnvelope(payload)) throw new OperatorApiError(response.status, payload);
      throw new Error(`API request failed with status ${response.status}`);
    }
    return payload as T;
  }
  return {
    getOperations(query, signal) {
      const parameters = new URLSearchParams({ limit: String(query.limit ?? 25) });
      if (query.state) parameters.set("state", query.state);
      if (query.jobType) parameters.set("job_type", query.jobType);
      if (query.cursor) parameters.set("cursor", query.cursor);
      return request<OperationsResponse>(`/api/v1/operations?${parameters}`, { signal });
    },
    getCoverage(signal) {
      return request<CoverageResponse>("/api/v1/operations/coverage", { signal });
    },
    retryJob(jobId, idempotencyKey, signal) {
      return request<RetryJobResponse>(`/api/v1/jobs/${encodeURIComponent(jobId)}/retry`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        signal,
      });
    },
  };
}
