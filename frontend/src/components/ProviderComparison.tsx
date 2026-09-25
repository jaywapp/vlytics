import { StatusBadge } from "./StatusBadge";
import {
  predictionView,
  providerLabel,
  providerStateLabel,
} from "../features/matches/model";
import type { ProviderOutcome } from "../features/matches/types";

const percent = new Intl.NumberFormat("ko-KR", {
  style: "percent",
  maximumFractionDigits: 1,
});

export function ProviderComparison({
  outcomes,
  selectedProvider,
  onSelect,
}: {
  outcomes: ProviderOutcome[];
  selectedProvider: string;
  onSelect: (provider: string) => void;
}) {
  return (
    <div className="table-scroll">
      <table className="provider-table">
        <caption>통계 · GPT · Claude · Gemini를 같은 서버 Snapshot 기준으로 비교합니다.</caption>
        <thead>
          <tr>
            <th scope="col">모델</th>
            <th scope="col">홈 승리</th>
            <th scope="col">실행 상태</th>
            <th scope="col">실행 모델 / 버전</th>
            <th scope="col">보기</th>
          </tr>
        </thead>
        <tbody>
          {outcomes.map((outcome) => {
            const view = predictionView(outcome.output);
            const failed = outcome.status !== "succeeded";
            return (
              <tr key={outcome.provider}>
                <th scope="row">{providerLabel(outcome.provider)}</th>
                <td className="numeric">
                  {failed
                    ? "계산 불가"
                    : view.homeWinProbability === null
                      ? "미지원"
                      : percent.format(view.homeWinProbability)}
                </td>
                <td>
                  <StatusBadge tone={failed ? "warning" : "neutral"}>
                    {providerStateLabel(outcome)}
                  </StatusBadge>
                  {outcome.error_code ? <span className="error-code">{outcome.error_code}</span> : null}
                </td>
                <td className="model-identity numeric">
                  <span>{outcome.resolved_model_id ?? outcome.requested_model ?? "기록 없음"}</span>
                  <small>
                    버전 {outcome.model_version ?? outcome.output?.model_version ?? "기록 없음"}
                  </small>
                </td>
                <td>
                  <button
                    type="button"
                    className={selectedProvider === outcome.provider ? "model-button model-button--active" : "model-button"}
                    aria-pressed={selectedProvider === outcome.provider}
                    disabled={failed || !outcome.output}
                    onClick={() => onSelect(outcome.provider)}
                  >
                    분석 보기
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
