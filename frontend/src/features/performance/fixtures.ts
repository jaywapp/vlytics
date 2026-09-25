import type {
  PairedComparison,
  PerformanceApiClient,
  PerformanceMetrics,
  PerformanceQuery,
  PerformanceResponse,
  PerformanceRow,
} from "./types";

const baseCohort = {
  stage: "regular",
  feature_version: "feature-v2",
  availability_policy: "live_prospective",
  timing_eligibility: "on_time",
  result_finality: "corrected",
};

const emptyMetrics: PerformanceMetrics = {
  winner_n: 0,
  set_n: 0,
  winner_accuracy: null,
  brier: null,
  log_loss: null,
  set_rps: null,
  set_score_accuracy: null,
};

function paired(
  baseline_kind: PairedComparison["baseline_kind"],
  paired_n: number,
  baseline_model_version: string,
  brier: number | null,
  logLoss: number | null,
): PairedComparison {
  const standardError = paired_n > 1 ? 0.017 : null;
  const metric = (
    name: "brier" | "log_loss",
    difference: number | null,
  ) => ({
    metric: name,
    sample_size: paired_n,
    mean_difference: difference,
    standard_error: standardError,
    confidence_lower:
      difference === null || standardError === null ? null : difference - 1.96 * standardError,
    confidence_upper:
      difference === null || standardError === null ? null : difference + 1.96 * standardError,
    version: "paired-common-match-normal-95-v1" as const,
  });
  return {
    baseline_kind,
    baseline_model_version,
    ai_individual_n: paired_n === 0 ? 20 : 20,
    baseline_individual_n: paired_n,
    paired_n,
    excluded_reasons: paired_n < 20 ? { baseline_unavailable: 20 - paired_n } : {},
    brier: metric("brier", brier),
    log_loss: metric("log_loss", logLoss),
  };
}

const rows: PerformanceRow[] = [
  {
    provider: "openai",
    model_version: "gpt-5.6-sol-2026-09",
    prompt_version: "performance-prompt-v3",
    prediction_type: "llm",
    division: "women",
    competition: "regular-2026",
    cohort: { ...baseCohort, division: "women", competition: "regular-2026", provider: "openai", model_version: "gpt-5.6-sol-2026-09", prompt_version: "performance-prompt-v3" },
    sample_size: 20,
    failure_count: 1,
    excluded_count: 2,
    corrected_evaluation_count: 2,
    paired_sample_size: 18,
    market_availability: "available",
    evaluator_version: "result-evaluator-v1",
    cohort_policy_version: "performance-cohort-v1",
    evaluation_revision_ids: ["evaluation-r2-corrected"],
    result_revision_ids: ["result-r2-corrected"],
    metrics: {
      winner_n: 20,
      set_n: 20,
      winner_accuracy: 0.7,
      brier: 0.1812,
      log_loss: 0.5481,
      set_rps: 0.1432,
      set_score_accuracy: 0.42,
    },
    calibration: [
      { lower_bound: 0, upper_bound: 0.2, includes_upper_bound: false, mean_probability: 0.14, observed_rate: 0.1, sample_size: 4, uncertainty_lower: 0.018, uncertainty_upper: 0.404, interval_version: "fixed-width-0.2-wilson-95-v1" },
      { lower_bound: 0.2, upper_bound: 0.4, includes_upper_bound: false, mean_probability: 0.32, observed_rate: 0.4, sample_size: 5, uncertainty_lower: 0.118, uncertainty_upper: 0.769, interval_version: "fixed-width-0.2-wilson-95-v1" },
      { lower_bound: 0.4, upper_bound: 0.6, includes_upper_bound: false, mean_probability: 0.51, observed_rate: 0.5, sample_size: 4, uncertainty_lower: 0.15, uncertainty_upper: 0.85, interval_version: "fixed-width-0.2-wilson-95-v1" },
      { lower_bound: 0.6, upper_bound: 0.8, includes_upper_bound: false, mean_probability: 0.69, observed_rate: 0.8, sample_size: 5, uncertainty_lower: 0.376, uncertainty_upper: 0.964, interval_version: "fixed-width-0.2-wilson-95-v1" },
      { lower_bound: 0.8, upper_bound: 1, includes_upper_bound: true, mean_probability: 0.86, observed_rate: 1, sample_size: 2, uncertainty_lower: 0.342, uncertainty_upper: 1, interval_version: "fixed-width-0.2-wilson-95-v1" },
    ],
    comparisons: [
      paired("home_rate", 18, "home-rate-v1", -0.0214, -0.038),
      paired("statistical", 18, "walk-forward-stat-v2", -0.018, -0.03),
      paired("market", 0, "available-market-v1", null, null),
    ],
  },
  {
    provider: "openai",
    model_version: "gpt-5.6-sol-2026-10",
    prompt_version: "performance-prompt-v4",
    prediction_type: "llm",
    division: "women",
    competition: "regular-2026",
    cohort: { ...baseCohort, division: "women", competition: "regular-2026", provider: "openai", model_version: "gpt-5.6-sol-2026-10", prompt_version: "performance-prompt-v4", result_finality: "final" },
    sample_size: 1,
    failure_count: 0,
    excluded_count: 0,
    corrected_evaluation_count: 0,
    paired_sample_size: 1,
    market_availability: "missing",
    evaluator_version: "result-evaluator-v1",
    cohort_policy_version: "performance-cohort-v1",
    evaluation_revision_ids: ["evaluation-single"],
    result_revision_ids: ["result-final"],
    metrics: { ...emptyMetrics, winner_n: 1, winner_accuracy: 1, brier: 0.09, log_loss: 0.3567 },
    calibration: [],
    comparisons: [paired("statistical", 1, "walk-forward-stat-v2", -0.01, -0.02)],
  },
  {
    provider: "anthropic",
    model_version: "claude-synthetic-empty",
    prompt_version: "performance-prompt-v3",
    prediction_type: "llm",
    division: "women",
    competition: "regular-2026",
    cohort: { ...baseCohort, division: "women", competition: "regular-2026", provider: "anthropic", model_version: "claude-synthetic-empty", prompt_version: "performance-prompt-v3" },
    sample_size: 0,
    failure_count: 3,
    excluded_count: 3,
    corrected_evaluation_count: 0,
    paired_sample_size: 0,
    market_availability: "missing",
    evaluator_version: "result-evaluator-v1",
    cohort_policy_version: "performance-cohort-v1",
    evaluation_revision_ids: [],
    result_revision_ids: [],
    metrics: emptyMetrics,
    calibration: [],
    comparisons: [paired("statistical", 0, "walk-forward-stat-v2", null, null)],
  },
  ...([
    ["home_rate", "home-rate-v1", 20, 0.55, 0.25, 0.6931, "not_supported"],
    ["statistical", "walk-forward-stat-v2", 19, 0.632, 0.2026, 0.6014, "not_supported"],
    ["market", "available-market-v1", 0, null, null, null, "missing"],
  ] as const).map(([prediction_type, model_version, sample_size, accuracy, brier, logLoss, market_availability]) => ({
    provider: "baseline",
    model_version,
    prompt_version: "not-applicable",
    prediction_type,
    division: "women" as const,
    competition: "regular-2026",
    cohort: { ...baseCohort, division: "women", competition: "regular-2026", provider: "baseline", model_version, prompt_version: "not-applicable" },
    sample_size,
    failure_count: 0,
    excluded_count: sample_size === 0 ? 20 : 20 - sample_size,
    corrected_evaluation_count: 0,
    paired_sample_size: 0,
    market_availability,
    evaluator_version: "result-evaluator-v1",
    cohort_policy_version: "performance-cohort-v1",
    evaluation_revision_ids: [],
    result_revision_ids: [],
    metrics: { ...emptyMetrics, winner_n: sample_size, winner_accuracy: accuracy, brier, log_loss: logLoss },
    calibration: [],
    comparisons: [],
  })),
  {
    provider: "openai",
    model_version: "gpt-5.6-sol-2026-09",
    prompt_version: "performance-prompt-v3",
    prediction_type: "llm",
    division: "men",
    competition: "cup-2026",
    cohort: { ...baseCohort, division: "men", competition: "cup-2026", provider: "openai", model_version: "gpt-5.6-sol-2026-09", prompt_version: "performance-prompt-v3", stage: "other" },
    sample_size: 8,
    failure_count: 1,
    excluded_count: 1,
    corrected_evaluation_count: 0,
    paired_sample_size: 7,
    market_availability: "unverified",
    evaluator_version: "result-evaluator-v1",
    cohort_policy_version: "performance-cohort-v1",
    evaluation_revision_ids: ["evaluation-men"],
    result_revision_ids: ["result-men"],
    metrics: { ...emptyMetrics, winner_n: 8, set_n: 8, winner_accuracy: 0.625, brier: 0.213, log_loss: 0.617, set_rps: 0.172, set_score_accuracy: 0.375 },
    calibration: [],
    comparisons: [paired("statistical", 7, "walk-forward-stat-v2", -0.01, -0.015)],
  },
];

function matches(row: PerformanceRow, query: PerformanceQuery): boolean {
  return (
    (!query.division || row.division === query.division) &&
    (!query.competition || row.competition === query.competition) &&
    (!query.provider || row.provider === query.provider) &&
    (!query.model || row.model_version === query.model) &&
    (!query.prediction_type || row.prediction_type === query.prediction_type) &&
    (!query.prompt_version || row.prompt_version === query.prompt_version) &&
    (!query.stage || row.cohort.stage === query.stage) &&
    (!query.feature_version || row.cohort.feature_version === query.feature_version) &&
    (!query.availability_policy || row.cohort.availability_policy === query.availability_policy) &&
    (!query.timing_eligibility || row.cohort.timing_eligibility === query.timing_eligibility) &&
    (!query.result_finality || row.cohort.result_finality === query.result_finality) &&
    (!query.evaluator_version || row.evaluator_version === query.evaluator_version)
  );
}

export function createSyntheticPerformanceClient(options?: {
  error?: Error;
  empty?: boolean;
}): PerformanceApiClient {
  return {
    dataMode: "synthetic-test",
    getPerformance(query): Promise<PerformanceResponse> {
      if (options?.error) return Promise.reject(options.error);
      const items = options?.empty ? [] : rows.filter((row) => matches(row, query));
      return Promise.resolve({
        metadata: {
          schema_version: "vlytics.operator.v1",
          source_snapshot_ids: ["source-synthetic-020"],
          schedule_revision_ids: ["schedule-synthetic-020"],
          prediction_revision_ids: ["prediction-synthetic-020"],
          evaluation_revision_ids: ["evaluation-r2-corrected", "evaluation-single"],
          model_versions: {
            openai: ["gpt-5.6-sol-2026-09", "gpt-5.6-sol-2026-10"],
            baseline: ["home-rate-v1", "walk-forward-stat-v2", "available-market-v1"],
          },
        },
        data: { cohort_policy_version: "performance-cohort-v1", items },
      });
    },
  };
}
