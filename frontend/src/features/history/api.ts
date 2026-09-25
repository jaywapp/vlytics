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
      return payload as PredictionHistoryResponse;
    },
  };
}
