import type { Division, PredictionLifecycleStatus, ProviderStatus, RevisionMetadata } from "../matches/types";

export type PredictionHistoryItem = {
  id: string;
  record_type: "prediction" | "attempt";
  prediction_revision_id: string | null;
  attempt_id: string | null;
  match_id: string;
  competition: string;
  division: Division;
  provider: string;
  variant_id: string;
  prediction_type: string;
  requested_model: string;
  resolved_model_id: string | null;
  model_version: string | null;
  prompt_version: string;
  feature_version: string;
  schedule_revision_id: string;
  source_snapshot_id: string;
  generated_at: string;
  status: PredictionLifecycleStatus | ProviderStatus;
  output: Record<string, unknown>;
  evaluation_revision_id: string | null;
};

export type PredictionHistoryResponse = {
  metadata: RevisionMetadata;
  data: { items: PredictionHistoryItem[]; next_cursor: string | null };
};

export type HistoryQuery = {
  division?: Division;
  team?: string;
  competition?: string;
  provider?: string;
  model?: string;
  predictionType?: string;
  promptVersion?: string;
  startAt?: string;
  endAt?: string;
  cursor?: string;
  limit?: number;
};

export interface HistoryApiClient {
  getPredictions: (query: HistoryQuery, signal?: AbortSignal) => Promise<PredictionHistoryResponse>;
}
