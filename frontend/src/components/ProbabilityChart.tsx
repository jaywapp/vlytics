import type { SetScoreProbability } from "../features/matches/types";

const percent = new Intl.NumberFormat("ko-KR", {
  style: "percent",
  maximumFractionDigits: 1,
});

export function ProbabilityChart({
  probabilities,
  homeTeam,
}: {
  probabilities: SetScoreProbability[];
  homeTeam: string;
}) {
  const summary = probabilities.map((item) => `${item.outcome} ${percent.format(item.probability)}`).join(", ");

  return (
    <figure className="probability-figure">
      <div
        className="probability-chart"
        role="img"
        aria-label={`${homeTeam} 기준 세트 스코어 확률: ${summary}`}
      >
        {probabilities.map((item) => (
          <div className="probability-column" key={item.outcome}>
            <b className="numeric">{percent.format(item.probability)}</b>
            <div className="probability-track" aria-hidden="true">
              <span style={{ height: `${Math.max(item.probability * 100, 1)}%` }} />
            </div>
            <span>{item.outcome}</span>
          </div>
        ))}
      </div>
      <figcaption>{homeTeam} 기준 · 서버에 저장된 여섯 결과의 확률, 합계 100%</figcaption>
    </figure>
  );
}
