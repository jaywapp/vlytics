import { isIsoTimestamp, isNullableString, isRecord, isRevisionMetadata } from "../apiValidation";
import { OperatorApiError } from "../matches/api";
import type { ApiErrorEnvelope } from "../matches/types";
import type { HistoryApiClient, HistoryQuery, PredictionHistoryResponse } from "./types";

function isErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (!value || typeof value !== "object") return false;
  const error = value as Partial<ApiErrorEnvelope>;
  return (
    typeof error.code === "string" &&
    typeof error.message === "string" &&
    typeof error.retryable === "boolean" &&
    typeof error.correlation_id === "string"
  );
}

const historyStatuses = new Set([
  "published",
  "voided",
  "superseded",
  "late_rejected",
  "succeeded",
  "failed",
  "timed_out",
  "budget_skipped",
  "missing",
]);

function isHistoryItem(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === "string" &&
    (value.record_type === "prediction" || value.record_type === "attempt") &&
    isNullableString(value.prediction_revision_id) &&
    isNullableString(value.attempt_id) &&
    typeof value.match_id === "string" &&
    typeof value.competition === "string" &&
    (value.division === "men" || value.division === "women") &&
    typeof value.provider === "string" &&
    typeof value.variant_id === "string" &&
    typeof value.prediction_type === "string" &&
    typeof value.requested_model === "string" &&
    isNullableString(value.resolved_model_id) &&
    isNullableString(value.model_version) &&
    typeof value.prompt_version === "string" &&
    typeof value.feature_version === "string" &&
    typeof value.schedule_revision_id === "string" &&
    typeof value.source_snapshot_id === "string" &&
    isIsoTimestamp(value.generated_at) &&
    typeof value.status === "string" &&
    historyStatuses.has(value.status) &&
    isRecord(value.output) &&
    isNullableString(value.evaluation_revision_id)
  );
}

function isHistoryResponse(value: unknown): value is PredictionHistoryResponse {
  if (!isRecord(value) || !isRevisionMetadata(value.metadata) || !isRecord(value.data)) return false;
  return (
    Array.isArray(value.data.items) &&
    value.data.items.every(isHistoryItem) &&
    isNullableString(value.data.next_cursor)
  );
}

export function createHistoryApiClient({ token, baseUrl = "" }: { token: string; baseUrl?: string }): HistoryApiClient {
  return {
    async getPredictions(query: HistoryQuery, signal?: AbortSignal) {
      const parameters = new URLSearchParams({ limit: String(query.limit ?? 25) });
      const values: Array<[string, string | undefined]> = [
        ["division", query.division],
        ["team", query.team],
        ["competition", query.competition],
        ["provider", query.provider],
        ["model", query.model],
        ["prediction_type", query.predictionType],
        ["prompt_version", query.promptVersion],
        ["start_at", query.startAt],
        ["end_at", query.endAt],
        ["cursor", query.cursor],
      ];
      values.forEach(([key, value]) => value && parameters.set(key, value));
      const response = await fetch(`${baseUrl.replace(/\/$/, "")}/api/v1/predictions?${parameters}`, {
        headers: { Authorization: `Bearer ${token}` },
        signal,
      });
      const payload: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        if (isErrorEnvelope(payload)) throw new OperatorApiError(response.status, payload);
        throw new Error(`API request failed with status ${response.status}`);
      }
      if (!isHistoryResponse(payload)) {
        throw new Error("API response does not match the prediction history envelope");
      }
      return payload;
    },
  };
}
