import {
  isIsoTimestamp,
  isNullableString,
  isRecord,
  isRevisionMetadata,
} from "../apiValidation";
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

function isNullableTimestamp(value: unknown): value is string | null {
  return value === null || isIsoTimestamp(value);
}

function isTeam(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.code === "string" &&
    typeof value.name === "string"
  );
}

const availabilities = new Set(["available", "missing", "not_supported", "unverified"]);
const marketAvailabilities = new Set([...availabilities, "stale", "late"]);

function isMarket(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.availability === "string" &&
    marketAvailabilities.has(value.availability) &&
    isNullableString(value.source) &&
    isNullableString(value.snapshot_id) &&
    isNullableTimestamp(value.quoted_at) &&
    isNullableString(value.reason)
  );
}

const providerStatuses = new Set(["succeeded", "failed", "timed_out", "budget_skipped", "missing"]);
const lifecycleStatuses = new Set(["published", "voided", "superseded", "late_rejected"]);

function isProviderOutcome(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.provider === "string" &&
    typeof value.status === "string" &&
    providerStatuses.has(value.status) &&
    isNullableString(value.variant_id) &&
    isNullableString(value.requested_model) &&
    isNullableString(value.resolved_model_id) &&
    isNullableString(value.model_version) &&
    isNullableString(value.prompt_version) &&
    isNullableTimestamp(value.generated_at) &&
    isNullableString(value.prediction_revision_id) &&
    isNullableString(value.attempt_id) &&
    (value.lifecycle_status === null ||
      (typeof value.lifecycle_status === "string" && lifecycleStatuses.has(value.lifecycle_status))) &&
    isNullableString(value.schedule_revision_id) &&
    isNullableString(value.feature_snapshot_id) &&
    isNullableString(value.feature_version) &&
    isNullableTimestamp(value.input_cutoff_at) &&
    isMarket(value.market) &&
    (value.output === null || isRecord(value.output)) &&
    isNullableString(value.error_code)
  );
}

function isMatchSummary(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === "string" &&
    typeof value.competition === "string" &&
    (value.division === "men" || value.division === "women") &&
    typeof value.stage === "string" &&
    isIsoTimestamp(value.scheduled_start_at_utc) &&
    isIsoTimestamp(value.scheduled_start_at_local) &&
    (value.timezone === "UTC" || value.timezone === "Asia/Seoul") &&
    typeof value.status === "string" &&
    isTeam(value.home_team) &&
    isTeam(value.away_team) &&
    isMarket(value.market) &&
    Array.isArray(value.provider_outcomes) &&
    value.provider_outcomes.every(isProviderOutcome)
  );
}

function isScheduleResponse(value: unknown): value is ScheduleResponse {
  if (!isRecord(value) || !isRevisionMetadata(value.metadata) || !isRecord(value.data)) {
    return false;
  }
  return (
    typeof value.data.date === "string" &&
    (value.data.timezone === "UTC" || value.data.timezone === "Asia/Seoul") &&
    Array.isArray(value.data.items) &&
    value.data.items.every(isMatchSummary) &&
    isNullableString(value.data.next_cursor)
  );
}

function isMatchDetailResponse(value: unknown): value is MatchDetailResponse {
  if (
    !isRecord(value) ||
    !isRevisionMetadata(value.metadata) ||
    !isRecord(value.data) ||
    !isMatchSummary(value.data)
  ) {
    return false;
  }
  return (
    isNullableTimestamp(value.data.actual_start_at_utc) &&
    isNullableString(value.data.venue) &&
    (value.data.result === null || isRecord(value.data.result)) &&
    isNullableString(value.data.feature_snapshot_id) &&
    isNullableString(value.data.feature_version) &&
    isRecord(value.data.source_coverage) &&
    Object.values(value.data.source_coverage).every(
      (availability) => typeof availability === "string" && availabilities.has(availability),
    )
  );
}

export function createOperatorApiClient({ token, baseUrl = "" }: ClientOptions): OperatorApiClient {
  const normalizedBase = baseUrl.replace(/\/$/, "");

  async function request<T>(
    path: string,
    validate: (value: unknown) => value is T,
    signal?: AbortSignal,
  ): Promise<T> {
    const response = await fetch(`${normalizedBase}${path}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal,
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      if (isErrorEnvelope(payload)) throw new OperatorApiError(response.status, payload);
      throw new Error(`API request failed with status ${response.status}`);
    }
    if (!validate(payload)) throw new Error("API response does not match the operator envelope");
    return payload;
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
      if (query.cursor) parameters.set("cursor", query.cursor);
      return request<ScheduleResponse>(
        `/api/v1/schedule?${parameters.toString()}`,
        isScheduleResponse,
        signal,
      );
    },
    getMatch(matchId, signal) {
      return request<MatchDetailResponse>(
        `/api/v1/matches/${encodeURIComponent(matchId)}?timezone=Asia%2FSeoul`,
        isMatchDetailResponse,
        signal,
      );
    },
  };
}
