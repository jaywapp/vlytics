import type { CalibrationBin, PerformanceRow } from "./types";

export const PRIMARY_METRICS = [
  { key: "winner_accuracy", label: "승자 Accuracy", unit: "rate", direction: "높을수록 좋음" },
  { key: "brier", label: "Brier", unit: "score", direction: "낮을수록 좋음" },
  { key: "log_loss", label: "Log Loss", unit: "score", direction: "낮을수록 좋음" },
  { key: "set_rps", label: "세트 RPS", unit: "score", direction: "낮을수록 좋음" },
  {
    key: "set_score_accuracy",
    label: "세트 Accuracy",
    unit: "rate",
    direction: "높을수록 좋음",
  },
] as const;

export function modelLabel(row: PerformanceRow): string {
  if (row.provider === "baseline") {
    if (row.prediction_type === "home_rate") return "홈 승률 기준선";
    if (row.prediction_type === "statistical") return "통계 기준선";
    if (row.prediction_type === "market") return "가용 시장 기준선";
  }
  return `${row.provider} · ${row.model_version}`;
}

export function formatMetric(value: number | null | undefined, unit: "rate" | "score"): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "산출 불가";
  return unit === "rate" ? `${(value * 100).toFixed(1)}%` : value.toFixed(4);
}

export function formatSignedMetric(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "산출 불가";
  return `${value >= 0 ? "+" : ""}${value.toFixed(4)}`;
}

export function calibrationLabel(bin: CalibrationBin): string {
  return `${Math.round(bin.lower_bound * 100)}–${Math.round(bin.upper_bound * 100)}%`;
}

export function isBaseline(row: PerformanceRow): boolean {
  return row.provider === "baseline";
}

export function marketLabel(row: PerformanceRow): string {
  const labels = {
    available: "시장 데이터 사용 가능",
    missing: "시장 데이터 미수신",
    not_supported: "시장 데이터 미지원",
    unverified: "시장 데이터 검증 전",
  } as const;
  return labels[row.market_availability];
}
