import type {
  Availability,
  MatchDetail,
  PredictionOutput,
  ProviderOutcome,
  SetScoreOutcome,
  SetScoreProbability,
} from "./types";

const SET_OUTCOMES: SetScoreOutcome[] = ["3:0", "3:1", "3:2", "2:3", "1:3", "0:3"];
const PROVIDER_LABELS: Record<string, string> = {
  statistical: "통계 세트 모델",
  statistics: "통계 세트 모델",
  stat: "통계 세트 모델",
  openai: "GPT 독립 실험군",
  anthropic: "Claude 독립 실험군",
  google: "Gemini 독립 실험군",
  gemini: "Gemini 독립 실험군",
};

export type PredictionView = {
  homeWinProbability: number | null;
  setScores: SetScoreProbability[] | null;
  capabilities: string[];
  rationale: string | null;
  risks: string[];
  confidenceNote: string | null;
  jointDistributionRef: string | null;
  pointsAvailability: Availability;
};

function validProbability(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function parseSetScores(value: unknown): SetScoreProbability[] | null {
  if (!Array.isArray(value) || value.length !== SET_OUTCOMES.length) return null;
  const parsed = value.map((item, index) => {
    if (!item || typeof item !== "object") return null;
    const candidate = item as Partial<SetScoreProbability>;
    if (candidate.outcome !== SET_OUTCOMES[index] || !validProbability(candidate.probability)) return null;
    return { outcome: candidate.outcome, probability: candidate.probability };
  });
  if (parsed.some((item) => item === null)) return null;
  const scores = parsed as SetScoreProbability[];
  const total = scores.reduce((sum, item) => sum + item.probability, 0);
  return Math.abs(total - 1) <= 1e-6 ? scores : null;
}

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider.toLowerCase()] ?? provider;
}

export function providerStateLabel(outcome: ProviderOutcome): string {
  const labels: Record<ProviderOutcome["status"], string> = {
    succeeded: "저장 완료",
    failed: "실패",
    timed_out: "시간 초과",
    budget_skipped: "예산으로 건너뜀",
    missing: "응답 없음",
  };
  return labels[outcome.status];
}

export function predictionView(output: PredictionOutput | null): PredictionView {
  const capabilities = Array.isArray(output?.capabilities)
    ? output.capabilities.filter((item): item is string => typeof item === "string")
    : [];
  const jointDistributionRef =
    typeof output?.joint_score_distribution_ref === "string" ? output.joint_score_distribution_ref : null;
  const hasPointCapability =
    capabilities.includes("point_handicap") || capabilities.includes("point_total");
  return {
    homeWinProbability: validProbability(output?.home_win_probability)
      ? output.home_win_probability
      : null,
    setScores: parseSetScores(output?.set_score_probabilities),
    capabilities,
    rationale: typeof output?.rationale === "string" ? output.rationale : null,
    risks: Array.isArray(output?.risk_factors)
      ? output.risk_factors.filter((item): item is string => typeof item === "string")
      : [],
    confidenceNote: typeof output?.confidence_note === "string" ? output.confidence_note : null,
    jointDistributionRef,
    pointsAvailability: hasPointCapability
      ? jointDistributionRef
        ? "available"
        : "unverified"
      : "not_supported",
  };
}

export function matchStateLabel(status: string): string {
  const labels: Record<string, string> = {
    scheduled: "예정",
    started: "경기 중",
    finished: "종료",
    cancelled: "취소",
    postponed: "연기",
  };
  return labels[status] ?? status;
}

export function coverageSummary(match: MatchDetail): string {
  const unavailable = Object.entries(match.source_coverage).filter(([, value]) => value !== "available");
  if (unavailable.length === 0) return "확인된 원천 항목 사용 가능";
  return unavailable.map(([kind, state]) => `${kind}: ${availabilityLabel(state)}`).join(" · ");
}

export function availabilityLabel(value: Availability): string {
  const labels: Record<Availability, string> = {
    available: "사용 가능",
    missing: "미수신",
    not_supported: "미지원",
    unverified: "미검증",
  };
  return labels[value];
}
