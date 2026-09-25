export type Availability = "available" | "missing" | "not_supported" | "unverified";
export type Division = "men" | "women";
export type ProviderStatus = "succeeded" | "failed" | "timed_out" | "budget_skipped" | "missing";

export type RevisionMetadata = {
  schema_version: "vlytics.operator.v1";
  source_snapshot_ids: string[];
  schedule_revision_ids: string[];
  prediction_revision_ids: string[];
  evaluation_revision_ids: string[];
  model_versions: Record<string, string[]>;
};

export type TeamSummary = {
  id: string;
  code: string;
  name: string;
};

export type MarketSummary = {
  availability: Availability;
  source: string | null;
  snapshot_id: string | null;
  quoted_at: string | null;
  reason: string | null;
};

export type SetScoreOutcome = "3:0" | "3:1" | "3:2" | "2:3" | "1:3" | "0:3";

export type SetScoreProbability = {
  outcome: SetScoreOutcome;
  probability: number;
};

export type PredictionOutput = {
  schema_version?: string;
  producer_variant_id?: string;
  model_version?: string;
  distribution_version?: string;
  capabilities?: string[];
  home_win_probability?: number;
  set_score_probabilities?: SetScoreProbability[];
  joint_score_distribution_ref?: string;
  rationale?: string;
  risk_factors?: string[];
  confidence_note?: string;
  target_provenance?: Record<string, unknown>;
};

export type ProviderOutcome = {
  provider: string;
  status: ProviderStatus;
  requested_model: string | null;
  resolved_model_id: string | null;
  model_version: string | null;
  prompt_version: string | null;
  generated_at: string | null;
  prediction_revision_id: string | null;
  output: PredictionOutput | null;
  error_code: string | null;
};

export type MatchSummary = {
  id: string;
  competition: string;
  division: Division;
  stage: string;
  scheduled_start_at_utc: string;
  scheduled_start_at_local: string;
  timezone: "UTC" | "Asia/Seoul";
  status: string;
  home_team: TeamSummary;
  away_team: TeamSummary;
  market: MarketSummary;
  provider_outcomes: ProviderOutcome[];
};

export type MatchDetail = MatchSummary & {
  actual_start_at_utc: string | null;
  venue: string | null;
  result: Record<string, unknown> | null;
  feature_snapshot_id: string | null;
  feature_version: string | null;
  source_coverage: Record<string, Availability>;
};

export type ScheduleResponse = {
  metadata: RevisionMetadata;
  data: {
    date: string;
    timezone: "UTC" | "Asia/Seoul";
    items: MatchSummary[];
    next_cursor: string | null;
  };
};

export type MatchDetailResponse = {
  metadata: RevisionMetadata;
  data: MatchDetail;
};

export type ApiErrorEnvelope = {
  code: string;
  message: string;
  retryable: boolean;
  correlation_id: string;
};

export type ScheduleQuery = {
  date: string;
  timezone: "Asia/Seoul";
  division?: Division;
};

export interface OperatorApiClient {
  readonly dataMode: "live" | "synthetic-test";
  getSchedule: (query: ScheduleQuery, signal?: AbortSignal) => Promise<ScheduleResponse>;
  getMatch: (matchId: string, signal?: AbortSignal) => Promise<MatchDetailResponse>;
}
