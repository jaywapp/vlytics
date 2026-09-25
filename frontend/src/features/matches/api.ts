import type {
  ApiErrorEnvelope,
  MatchDetailResponse,
  OperatorApiClient,
  ScheduleResponse,
} from "./types";

type ClientOptions = {
  token: string;
  baseUrl?: string;
};

export class OperatorApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly correlationId: string;
  readonly status: number;

  constructor(status: number, error: ApiErrorEnvelope) {
    super(error.message);
    this.name = "OperatorApiError";
    this.status = status;
    this.code = error.code;
    this.retryable = error.retryable;
    this.correlationId = error.correlation_id;
  }
}

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

function isEnvelope(value: unknown): value is { metadata: unknown; data: unknown } {
  return Boolean(value && typeof value === "object" && "metadata" in value && "data" in value);
}

export function createOperatorApiClient({ token, baseUrl = "" }: ClientOptions): OperatorApiClient {
  const normalizedBase = baseUrl.replace(/\/$/, "");

  async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
    const response = await fetch(`${normalizedBase}${path}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal,
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      if (isErrorEnvelope(payload)) throw new OperatorApiError(response.status, payload);
      throw new Error(`API request failed with status ${response.status}`);
    }
    if (!isEnvelope(payload)) throw new Error("API response does not match the operator envelope");
    return payload as T;
  }

  return {
    dataMode: "live",
    getSchedule(query, signal) {
      const parameters = new URLSearchParams({
        date: query.date,
        timezone: query.timezone,
        limit: "100",
      });
      if (query.division) parameters.set("division", query.division);
      return request<ScheduleResponse>(`/api/v1/schedule?${parameters.toString()}`, signal);
    },
    getMatch(matchId, signal) {
      return request<MatchDetailResponse>(
        `/api/v1/matches/${encodeURIComponent(matchId)}?timezone=Asia%2FSeoul`,
        signal,
      );
    },
  };
}
