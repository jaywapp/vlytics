import type { Availability, Division, RevisionMetadata } from "../matches/types";

export type PerformanceMetrics = {
  winner_n: number;
  set_n: number;
  winner_accuracy: number | null;
  brier: number | null;
  log_loss: number | null;
  set_rps: number | null;
  set_score_accuracy: number | null;
};

export type CalibrationBin = {
  lower_bound: number;
  upper_bound: number;
  includes_upper_bound: boolean;
  mean_probability: number;
  observed_rate: number;
  sample_size: number;
  uncertainty_lower: number;
  uncertainty_upper: number;
  interval_version: "fixed-width-0.2-wilson-95-v1";
};

export type PairedMetric = {
  metric: "brier" | "log_loss";
  sample_size: number;
  mean_difference: number | null;
  standard_error: number | null;
  confidence_lower: number | null;
  confidence_upper: number | null;
  version: "paired-common-match-normal-95-v1";
};

export type PairedComparison = {
  baseline_kind: "home_rate" | "statistical" | "market";
  baseline_model_version: string;
  ai_individual_n: number;
  baseline_individual_n: number;
  paired_n: number;
  excluded_reasons: Record<string, number>;
  brier: PairedMetric;
  log_loss: PairedMetric;
};

export type PerformanceRow = {
  provider: string;
  model_version: string;
  prompt_version: string;
  prediction_type: string;
  division: Division;
  competition: string;
  cohort: Record<string, string>;
  sample_size: number;
  failure_count: number;
  excluded_count: number;
  corrected_evaluation_count: number;
  paired_sample_size: number;
  metrics: PerformanceMetrics;
  calibration: CalibrationBin[];
  comparisons: PairedComparison[];
  market_availability: Availability;
  evaluator_version: string;
  cohort_policy_version: string;
  evaluation_revision_ids: string[];
  result_revision_ids: string[];
};

export type PerformanceResponse = {
  metadata: RevisionMetadata;
  data: {
    cohort_policy_version: string;
    items: PerformanceRow[];
  };
};

export type PerformanceQuery = {
  division?: Division;
  competition?: string;
  provider?: string;
  model?: string;
  prediction_type?: string;
  prompt_version?: string;
  stage?: string;
  feature_version?: string;
  availability_policy?:
    | "historical_reconstruction"
    | "historical_point_in_time"
    | "live_prospective";
  timing_eligibility?: "on_time" | "reconstructed" | "diagnostic";
  result_finality?: "provisional" | "final" | "corrected" | "void";
  evaluator_version?: string;
  start_at?: string;
  end_at?: string;
};

export interface PerformanceApiClient {
  readonly dataMode: "live" | "synthetic-test";
  getPerformance: (
    query: PerformanceQuery,
    signal?: AbortSignal,
  ) => Promise<PerformanceResponse>;
}
