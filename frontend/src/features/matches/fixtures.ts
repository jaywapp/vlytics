import type {
  MatchDetail,
  MatchDetailResponse,
  MatchSummary,
  OperatorApiClient,
  PredictionOutput,
  ProviderOutcome,
  RevisionMetadata,
  ScheduleResponse,
} from "./types";

const distributionRef = "a".repeat(64);

const fullOutput: PredictionOutput = {
  schema_version: "prediction-v1",
  producer_variant_id: "stat-set-v1",
  model_version: "set-v1.2.0",
  distribution_version: "set-distribution-v1",
  capabilities: ["winner", "set_score", "point_handicap", "point_total"],
  home_win_probability: 0.62,
  set_score_probabilities: [
    { outcome: "3:0", probability: 0.16 },
    { outcome: "3:1", probability: 0.24 },
    { outcome: "3:2", probability: 0.22 },
    { outcome: "2:3", probability: 0.16 },
    { outcome: "1:3", probability: 0.13 },
    { outcome: "0:3", probability: 0.09 },
  ],
  joint_score_distribution_ref: distributionRef,
  rationale: "최근 공격 효율과 휴식일 차이가 홈팀 쪽으로 기울었습니다.",
  risk_factors: [
    "출전 명단이 아직 확정되지 않았습니다.",
    "아주 긴 검증용 위험 설명도 레이아웃을 깨지 않고 여러 줄로 읽혀야 합니다.",
  ],
  confidence_note: "이 설명과 수치는 합성 fixture이며 실제 예측 성능을 나타내지 않습니다.",
};

function provider(
  name: string,
  status: ProviderOutcome["status"],
  output: PredictionOutput | null,
): ProviderOutcome {
  return {
    provider: name,
    status,
    requested_model: status === "succeeded" ? `${name}-synthetic` : null,
    resolved_model_id: status === "succeeded" ? `${name}-synthetic-2026-09` : null,
    model_version: status === "succeeded" ? "2026-09" : null,
    prompt_version: status === "succeeded" ? "prompt-v1" : null,
    generated_at: status === "succeeded" ? "2026-09-20T09:30:00Z" : null,
    prediction_revision_id: status === "succeeded" ? `prediction-${name}-1` : null,
    output,
    error_code: status === "succeeded" ? null : "provider_timeout",
  };
}

const outcomes = [
  provider("statistical", "succeeded", fullOutput),
  provider("openai", "succeeded", {
    ...fullOutput,
    producer_variant_id: "gpt-independent-v1",
    model_version: "gpt-synthetic-v1",
    home_win_probability: 0.59,
  }),
  provider("anthropic", "timed_out", null),
  provider("google", "succeeded", {
    schema_version: "prediction-v1",
    producer_variant_id: "gemini-independent-v1",
    model_version: "gemini-synthetic-v1",
    distribution_version: "winner-v1",
    capabilities: ["winner"],
    home_win_probability: 0.57,
    rationale: "서버가 승패 대상만 저장한 합성 응답입니다.",
    risk_factors: ["세트 스코어 대상은 지원하지 않습니다."],
  }),
];

const secondOutcomes = [
  provider("statistical", "succeeded", {
    ...fullOutput,
    home_win_probability: 0.48,
    rationale: "긴 팀 이름에서도 서버 근거를 그대로 표시합니다.",
  }),
  provider("openai", "failed", null),
];

function summary(
  id: string,
  homeName: string,
  awayName: string,
  time: string,
  providerOutcomes: ProviderOutcome[],
  marketAvailable: boolean,
): MatchSummary {
  return {
    id,
    competition: "synthetic-regular-2026",
    division: "women",
    stage: "regular",
    scheduled_start_at_utc: time,
    scheduled_start_at_local: time,
    timezone: "Asia/Seoul",
    status: "scheduled",
    home_team: { id: `home-${id}`, code: "HOME", name: homeName },
    away_team: { id: `away-${id}`, code: "AWAY", name: awayName },
    market: marketAvailable
      ? {
          availability: "available",
          source: "synthetic-contract",
          snapshot_id: `market-${id}`,
          quoted_at: "2026-09-20T09:20:00Z",
          reason: null,
        }
      : {
          availability: "missing",
          source: null,
          snapshot_id: null,
          quoted_at: null,
          reason: "market_source_not_configured_or_no_eligible_quote",
        },
    provider_outcomes: providerOutcomes,
  };
}

const matches = [
  summary("match-1", "한강 블루웨이브", "해오름 스파이크", "2026-09-20T10:30:00Z", outcomes, true),
  summary(
    "match-2",
    "매우 긴 이름을 가진 합성 홈 배구단 테스트 클럽",
    "동일하게 긴 이름을 가진 합성 원정 배구단",
    "2026-09-20T12:00:00Z",
    secondOutcomes,
    false,
  ),
];

function detail(match: MatchSummary): MatchDetail {
  return {
    ...match,
    actual_start_at_utc: null,
    venue: "합성 체육관",
    result: null,
    feature_snapshot_id: `feature-${match.id}`,
    feature_version: "feature-v2",
    source_coverage: {
      schedule: "available",
      lineup: "unverified",
      player_stats: "not_supported",
    },
  };
}

function metadata(match: MatchSummary): RevisionMetadata {
  return {
    schema_version: "vlytics.operator.v1",
    source_snapshot_ids: [`source-${match.id}`],
    schedule_revision_ids: [`schedule-${match.id}`],
    prediction_revision_ids: match.provider_outcomes
      .map((outcome) => outcome.prediction_revision_id)
      .filter((value): value is string => value !== null),
    evaluation_revision_ids: [],
    model_versions: Object.fromEntries(
      match.provider_outcomes
        .filter((outcome) => outcome.model_version)
        .map((outcome) => [outcome.provider, [outcome.model_version as string]]),
    ),
  };
}

export function createSyntheticFixtureClient(options?: {
  empty?: boolean;
  scheduleError?: Error;
  detailError?: Error;
}): OperatorApiClient {
  return {
    dataMode: "synthetic-test",
    getSchedule(query) {
      if (options?.scheduleError) return Promise.reject(options.scheduleError);
      const selected = options?.empty
        ? []
        : matches.filter((match) => !query.division || match.division === query.division);
      const response: ScheduleResponse = {
        metadata: metadata(matches[0]),
        data: {
          date: query.date,
          timezone: "Asia/Seoul",
          items: selected,
          next_cursor: null,
        },
      };
      return Promise.resolve(response);
    },
    getMatch(matchId) {
      if (options?.detailError) return Promise.reject(options.detailError);
      const match = matches.find((item) => item.id === matchId);
      if (!match) return Promise.reject(new Error("match was not found"));
      const response: MatchDetailResponse = { metadata: metadata(match), data: detail(match) };
      return Promise.resolve(response);
    },
  };
}
