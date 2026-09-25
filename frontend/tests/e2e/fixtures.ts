import type { Page, Route } from "@playwright/test";

export const operatorToken = "e2e-operator-token";

export const metadata = {
  schema_version: "vlytics.operator.v1",
  source_snapshot_ids: ["source-e2e-1"],
  schedule_revision_ids: ["schedule-e2e-1"],
  prediction_revision_ids: ["prediction-e2e-1"],
  evaluation_revision_ids: ["evaluation-e2e-1"],
  model_versions: {
    openai: ["model-n20", "model-n1"],
    anthropic: ["model-n0"],
  },
} as const;

const successfulPrediction = {
  provider: "openai",
  status: "succeeded",
  requested_model: "model-n20",
  resolved_model_id: "model-n20-2026-09",
  model_version: "model-n20",
  prompt_version: "prompt-v3",
  generated_at: "2026-09-20T08:00:00Z",
  prediction_revision_id: "prediction-e2e-1",
  output: {
    capabilities: ["winner", "set_score"],
    home_win_probability: 0.61,
    set_score_probabilities: [
      { outcome: "3:0", probability: 0.15 },
      { outcome: "3:1", probability: 0.2 },
      { outcome: "3:2", probability: 0.26 },
      { outcome: "2:3", probability: 0.16 },
      { outcome: "1:3", probability: 0.14 },
      { outcome: "0:3", probability: 0.09 },
    ],
    rationale: "서버 fixture의 검증된 근거입니다.",
    risk_factors: ["출전 명단이 확정되지 않았습니다."],
  },
  error_code: null,
};

const match = {
  id: "match-e2e-1",
  competition: "regular-2026",
  division: "women",
  stage: "regular",
  scheduled_start_at_utc: "2026-09-20T10:30:00Z",
  scheduled_start_at_local: "2026-09-20T19:30:00+09:00",
  timezone: "Asia/Seoul",
  status: "scheduled",
  home_team: { id: "home-1", code: "HOME", name: "한강 블루웨이브" },
  away_team: { id: "away-1", code: "AWAY", name: "해오름 스파이크" },
  market: {
    availability: "missing",
    source: null,
    snapshot_id: null,
    quoted_at: null,
    reason: "market_source_not_configured",
  },
  provider_outcomes: [
    successfulPrediction,
    {
      provider: "anthropic",
      status: "timed_out",
      requested_model: null,
      resolved_model_id: null,
      model_version: null,
      prompt_version: null,
      generated_at: null,
      prediction_revision_id: null,
      output: null,
      error_code: "provider_timeout",
    },
  ],
};

export const scheduleResponse = {
  metadata,
  data: {
    date: "2026-09-20",
    timezone: "Asia/Seoul",
    items: [match],
    next_cursor: null,
  },
};

export const emptyScheduleResponse = {
  ...scheduleResponse,
  data: { ...scheduleResponse.data, items: [] },
};

export const matchResponse = {
  metadata,
  data: {
    ...match,
    actual_start_at_utc: null,
    venue: "E2E 체육관",
    result: null,
    feature_snapshot_id: "feature-e2e-1",
    feature_version: "feature-v2",
    source_coverage: {
      schedule: "available",
      lineup: "missing",
      player_stats: "not_supported",
    },
  },
};

export const historyItem = {
  id: "prediction-e2e-1",
  match_id: "match-e2e-1",
  competition: "regular-2026",
  division: "women",
  provider: "openai",
  prediction_type: "winner",
  requested_model: "model-n20",
  resolved_model_id: "model-n20-2026-09",
  model_version: "model-n20",
  prompt_version: "prompt-v3",
  feature_version: "feature-v2",
  schedule_revision_id: "schedule-e2e-1",
  source_snapshot_id: "source-e2e-1",
  generated_at: "2026-09-20T08:00:00Z",
  status: "succeeded",
  output: {
    home_win_probability: 0.61,
    capabilities: ["winner"],
    api_key: "must-never-render",
    raw_payload: "private-provider-response",
  },
  evaluation_revision_id: "evaluation-e2e-1",
};

export const historyResponse = {
  metadata,
  data: { items: [historyItem], next_cursor: "opaque-page-2" },
};

export const operationsResponse = {
  metadata,
  data: {
    items: [
      {
        id: "11111111-1111-4111-8111-111111111111",
        job_type: "provider.openai",
        state: "failed",
        due_at: "2026-09-20T08:00:00Z",
        deadline_at: "2099-09-20T09:00:00Z",
        attempt_no: 2,
        error_code: "provider_timeout",
        retryable: true,
      },
      {
        id: "22222222-2222-4222-8222-222222222222",
        job_type: "mirror.sync",
        state: "failed",
        due_at: "2026-09-20T08:00:00Z",
        deadline_at: "2020-09-20T09:00:00Z",
        attempt_no: 3,
        error_code: "deadline_expired",
        retryable: true,
      },
    ],
    budgets: [
      {
        provider: "openai",
        currency: "USD",
        period: "day",
        period_start: "2026-09-20",
        period_end: "2026-09-21",
        as_of: "2026-09-20T08:01:00Z",
        reserved_amount: "1.50000000",
        actual_amount: "0.70000000",
        effective_amount: "1.20000000",
        reservation_count: 2,
        settled_count: 1,
        outstanding_count: 1,
        conservative_charge_count: 0,
      },
    ],
    next_cursor: null,
  },
};

export const coverageResponse = {
  metadata,
  data: {
    items: [
      {
        data_kind: "lineup",
        availability: "missing",
        count: 2,
        latest_observed_at: "2026-09-20T07:55:00Z",
        evidence_codes: ["source_not_published"],
      },
    ],
  },
};

const emptyMetrics = {
  winner_n: 0,
  set_n: 0,
  winner_accuracy: null,
  brier: null,
  log_loss: null,
  set_rps: null,
  set_score_accuracy: null,
};

function paired(sampleSize: number) {
  const available = sampleSize >= 2;
  const metric = (name: "brier" | "log_loss", difference: number) => ({
    metric: name,
    sample_size: sampleSize,
    mean_difference: available ? difference : null,
    standard_error: available ? 0.017 : null,
    confidence_lower: available ? difference - 0.03332 : null,
    confidence_upper: available ? difference + 0.03332 : null,
    version: "paired-common-match-normal-95-v1",
  });
  return {
    baseline_kind: "statistical",
    baseline_model_version: "walk-forward-stat-v2",
    ai_individual_n: sampleSize,
    baseline_individual_n: sampleSize,
    paired_n: sampleSize,
    excluded_reasons: sampleSize < 20 ? { baseline_unavailable: 20 - sampleSize } : {},
    brier: metric("brier", -0.018),
    log_loss: metric("log_loss", -0.03),
  };
}

const basePerformanceRow = {
  provider: "openai",
  prompt_version: "prompt-v3",
  prediction_type: "llm",
  division: "women",
  competition: "regular-2026",
  cohort: {
    stage: "regular",
    feature_version: "feature-v2",
    availability_policy: "live_prospective",
    timing_eligibility: "on_time",
    result_finality: "corrected",
  },
  failure_count: 0,
  excluded_count: 0,
  corrected_evaluation_count: 0,
  market_availability: "missing",
  evaluator_version: "result-evaluator-v1",
  cohort_policy_version: "performance-cohort-v1",
  result_revision_ids: ["result-e2e-1"],
};

export const performanceRows = [
  {
    ...basePerformanceRow,
    model_version: "model-n20",
    sample_size: 20,
    paired_sample_size: 18,
    corrected_evaluation_count: 1,
    metrics: {
      ...emptyMetrics,
      winner_n: 20,
      set_n: 20,
      winner_accuracy: 0.7,
      brier: 0.1812,
      log_loss: 0.5481,
      set_rps: 0.1432,
      set_score_accuracy: 0.42,
    },
    calibration: [
      {
        lower_bound: 0,
        upper_bound: 0.2,
        includes_upper_bound: false,
        mean_probability: 0.14,
        observed_rate: 0.1,
        sample_size: 4,
        uncertainty_lower: 0.018,
        uncertainty_upper: 0.404,
        interval_version: "fixed-width-0.2-wilson-95-v1",
      },
    ],
    comparisons: [paired(18), { ...paired(0), baseline_kind: "market", baseline_model_version: "market-v1" }],
    evaluation_revision_ids: ["evaluation-e2e-corrected"],
  },
  {
    ...basePerformanceRow,
    model_version: "model-n1",
    sample_size: 1,
    paired_sample_size: 1,
    metrics: { ...emptyMetrics, winner_n: 1, winner_accuracy: 1, brier: 0.09, log_loss: 0.3567 },
    calibration: [],
    comparisons: [paired(1)],
    evaluation_revision_ids: ["evaluation-e2e-single"],
  },
  {
    ...basePerformanceRow,
    provider: "anthropic",
    model_version: "model-n0",
    sample_size: 0,
    paired_sample_size: 0,
    failure_count: 3,
    excluded_count: 3,
    metrics: emptyMetrics,
    calibration: [],
    comparisons: [paired(0)],
    evaluation_revision_ids: [],
  },
];

export function performanceResponse(rows = performanceRows) {
  return {
    metadata,
    data: { cohort_policy_version: "performance-cohort-v1", items: rows },
  };
}

export const retryResponse = {
  metadata,
  data: {
    job_id: operationsResponse.data.items[0].id,
    state: "retry_wait",
    due_at: "2026-09-20T08:05:00Z",
    deadline_at: "2099-09-20T09:00:00Z",
    idempotent_replay: true,
  },
};

export const retryableError = {
  code: "temporary_unavailable",
  message: "fixture service unavailable",
  retryable: true,
  correlation_id: "e2e-correlation-1",
};

export async function installOperatorSession(page: Page) {
  await page.addInitScript((token) => {
    window.sessionStorage.setItem("vlytics.operator-token", token);
  }, operatorToken);
}

export async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

export function expectOperatorAuth(route: Route) {
  const authorization = route.request().headers()["authorization"];
  if (authorization !== `Bearer ${operatorToken}`) {
    throw new Error(`Unexpected authorization header: ${authorization ?? "missing"}`);
  }
}
