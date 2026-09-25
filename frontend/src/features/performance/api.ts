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
  if (!value || typeof value !== "object") return false;
  const envelope = value as Partial<PerformanceResponse>;
  return Boolean(
    envelope.metadata &&
      envelope.data &&
      typeof envelope.data.cohort_policy_version === "string" &&
      Array.isArray(envelope.data.items),
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
