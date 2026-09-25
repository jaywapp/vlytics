import { useEffect, useMemo, useState } from "react";

import { StatePanel } from "../../components/StatePanel";
import { OperatorApiError } from "../matches/api";
import {
  calibrationLabel,
  formatMetric,
  formatSignedMetric,
  isBaseline,
  marketLabel,
  modelLabel,
  PRIMARY_METRICS,
} from "./model";
import type {
  PerformanceApiClient,
  PerformanceQuery,
  PerformanceResponse,
  PerformanceRow,
} from "./types";
import "../../styles/performance.css";

export type PerformancePageProps = {
  client: PerformanceApiClient;
  onSignOut?: () => void;
};

type FilterOptions = {
  divisions: Set<string>;
  competitions: Set<string>;
  providers: Set<string>;
  models: Set<string>;
  prompts: Set<string>;
  features: Set<string>;
  stages: Set<string>;
  availabilityPolicies: Set<string>;
  timingEligibilities: Set<string>;
  resultFinalities: Set<string>;
};

const emptyFilterOptions = (): FilterOptions => ({
  divisions: new Set(),
  competitions: new Set(),
  providers: new Set(),
  models: new Set(),
  prompts: new Set(),
  features: new Set(),
  stages: new Set(),
  availabilityPolicies: new Set(),
  timingEligibilities: new Set(),
  resultFinalities: new Set(),
});

const EMPTY_ROWS: PerformanceRow[] = [];

function mergeOptions(previous: FilterOptions, rows: PerformanceRow[]): FilterOptions {
  const cohortValues = (name: string) =>
    rows.map((row) => row.cohort[name]).filter((value): value is string => Boolean(value));
  return {
    divisions: new Set([...previous.divisions, ...rows.map((row) => row.division)]),
    competitions: new Set([...previous.competitions, ...rows.map((row) => row.competition)]),
    providers: new Set([...previous.providers, ...rows.map((row) => row.provider)]),
    models: new Set([...previous.models, ...rows.map((row) => row.model_version)]),
    prompts: new Set([...previous.prompts, ...rows.map((row) => row.prompt_version)]),
    features: new Set([...previous.features, ...cohortValues("feature_version")]),
    stages: new Set([...previous.stages, ...cohortValues("stage")]),
    availabilityPolicies: new Set([...previous.availabilityPolicies, ...cohortValues("availability_policy")]),
    timingEligibilities: new Set([...previous.timingEligibilities, ...cohortValues("timing_eligibility")]),
    resultFinalities: new Set([...previous.resultFinalities, ...cohortValues("result_finality")]),
  };
}
function CountSummary({ row }: { row: PerformanceRow }) {
  return (
    <dl className="performance-counts">
      <div><dt>평가 n</dt><dd className="numeric">{row.sample_size}</dd></div>
      <div><dt>짝비교 n</dt><dd className="numeric">{row.paired_sample_size}</dd></div>
      <div><dt>실패</dt><dd className="numeric">{row.failure_count}</dd></div>
      <div><dt>제외</dt><dd className="numeric">{row.excluded_count}</dd></div>
      <div><dt>정정 평가</dt><dd className="numeric">{row.corrected_evaluation_count}</dd></div>
    </dl>
  );
}

function MetricTable({ rows }: { rows: PerformanceRow[] }) {
  return (
    <div className="performance-table-scroll">
      <table className="performance-table">
        <caption>서버 집계 성능 지표. Accuracy는 높을수록, Brier·Log Loss·RPS는 낮을수록 좋습니다.</caption>
        <thead><tr><th scope="col">모델 / 기준선</th>{PRIMARY_METRICS.map((metric) => <th scope="col" key={metric.key}>{metric.label}<small>{metric.direction}</small></th>)}<th scope="col">평가 n</th><th scope="col">실패 / 제외</th><th scope="col">시장 상태</th></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.provider}-${row.model_version}-${row.prompt_version}-${row.prediction_type}-${row.evaluator_version}-${JSON.stringify(row.cohort)}`}>
              <th scope="row"><span>{modelLabel(row)}</span><small>{row.prompt_version} · {row.prediction_type}</small></th>
              {PRIMARY_METRICS.map((metric) => <td className="numeric" key={metric.key}>{formatMetric(row.metrics[metric.key], metric.unit)}</td>)}
              <td className="numeric">{row.sample_size}</td>
              <td className="numeric">{row.failure_count} / {row.excluded_count}</td>
              <td>{marketLabel(row)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CalibrationPanel({ row }: { row: PerformanceRow }) {
  const bins = row.calibration;
  return (
    <section className="performance-section" aria-labelledby="calibration-title">
      <div className="section-heading"><div><p className="eyebrow">Calibration</p><h2 id="calibration-title">예측 확률과 실제 관측률</h2></div><p>서버의 고정 구간과 95% 불확실성 범위를 그대로 표시합니다.</p></div>
      {bins.length === 0 ? (
        <StatePanel title="보정 집계가 없습니다" description="선택된 서버 코호트가 calibration 구간을 제공하지 않았습니다." />
      ) : (
        <>
          <div className="calibration-chart" role="img" aria-label={`${modelLabel(row)} calibration. ${bins.map((bin) => `${calibrationLabel(bin)} 예측 ${formatMetric(bin.mean_probability, "rate")}, 관측 ${formatMetric(bin.observed_rate, "rate")}, n ${bin.sample_size}`).join(". ")}`}>
            {bins.map((bin) => (
              <div className="calibration-column" key={`${bin.lower_bound}-${bin.upper_bound}`}>
                <div className="calibration-bars" aria-hidden="true"><span className="forecast-bar" style={{ height: `${bin.mean_probability * 100}%` }} /><span className="observed-bar" style={{ height: `${bin.observed_rate * 100}%` }} /></div>
                <span>{calibrationLabel(bin)}</span>
              </div>
            ))}
          </div>
          <div className="performance-table-scroll"><table className="performance-table compact"><caption>Calibration 차트의 접근 가능한 수치 표</caption><thead><tr><th scope="col">확률 구간</th><th scope="col">평균 예측</th><th scope="col">관측률</th><th scope="col">95% 구간</th><th scope="col">n</th></tr></thead><tbody>{bins.map((bin) => <tr key={`${bin.lower_bound}-${bin.upper_bound}`}><th scope="row">{calibrationLabel(bin)}</th><td className="numeric">{formatMetric(bin.mean_probability, "rate")}</td><td className="numeric">{formatMetric(bin.observed_rate, "rate")}</td><td className="numeric">{`${formatMetric(bin.uncertainty_lower, "rate")}–${formatMetric(bin.uncertainty_upper, "rate")}`}</td><td className="numeric">{bin.sample_size}</td></tr>)}</tbody></table></div>
        </>
      )}
    </section>
  );
}
function baselineLabel(kind: "home_rate" | "statistical" | "market"): string {
  return { home_rate: "홈 승률", statistical: "통계", market: "가용 시장" }[kind];
}

function PairedComparison({ row }: { row: PerformanceRow }) {
  const comparisons = row.comparisons;
  return (
    <section className="performance-section" aria-labelledby="paired-title">
      <div className="section-heading"><div><p className="eyebrow">Common-match comparison</p><h2 id="paired-title">같은 경기 짝비교</h2></div><p>차이 = 선택 모델 − 기준선. 음수면 Brier·Log Loss가 더 낮습니다.</p></div>
      <div className="paired-note" role="note"><strong className="numeric">개별 n {row.sample_size} · 최대 짝비교 n {row.paired_sample_size}</strong><span>서버가 match·schedule·snapshot·cutoff·result revision 교집합만 비교했습니다.</span></div>
      <div className="performance-table-scroll"><table className="performance-table compact"><caption>동일 경기 기준선 대비 평균 차이, 표준오차와 서버 산출 95% 신뢰구간</caption><thead><tr><th scope="col">기준선 / 지표</th><th scope="col">개별 n (모델/기준선)</th><th scope="col">평균 차이</th><th scope="col">표준오차</th><th scope="col">95% CI</th><th scope="col">짝비교 n</th></tr></thead><tbody>{comparisons.flatMap((comparison) => [comparison.brier, comparison.log_loss].map((metric) => (
        <tr key={`${comparison.baseline_kind}-${metric.metric}`}><th scope="row">{baselineLabel(comparison.baseline_kind)} · {metric.metric === "brier" ? "Brier" : "Log Loss"}<small>{comparison.baseline_model_version}</small></th><td className="numeric">{comparison.ai_individual_n} / {comparison.baseline_individual_n}</td><td className="numeric">{formatSignedMetric(metric.mean_difference)}</td><td className="numeric">{formatMetric(metric.standard_error, "score")}</td><td className="numeric">{metric.confidence_lower === null || metric.confidence_upper === null ? "산출 불가" : `${formatSignedMetric(metric.confidence_lower)}–${formatSignedMetric(metric.confidence_upper)}`}</td><td className="numeric">{comparison.paired_n}</td></tr>
      )))}</tbody></table></div>
      {comparisons.some((comparison) => comparison.paired_n < 2) && <p className="warning-strip" role="note">짝비교 n이 2보다 작은 기준선은 표준오차와 신뢰구간을 해석할 수 없습니다.</p>}
    </section>
  );
}
export function PerformancePage({ client, onSignOut }: PerformancePageProps) {
  const [query, setQuery] = useState<PerformanceQuery>({});
  const [response, setResponse] = useState<PerformanceResponse | null>(null);
  const [options, setOptions] = useState<FilterOptions>(emptyFilterOptions);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [requestRevision, setRequestRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    client.getPerformance(query, controller.signal).then((next) => {
      setResponse(next);
      setOptions((current) => mergeOptions(current, next.data.items));
      setLoading(false);
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      setError(reason instanceof Error ? reason : new Error("성능 데이터를 불러오지 못했습니다."));
      setLoading(false);
    });
    return () => controller.abort();
  }, [client, query, requestRevision]);

  const rows = response?.data.items ?? EMPTY_ROWS;
  const primary = useMemo(() => rows.find((row) => !isBaseline(row)) ?? rows[0], [rows]);
  const isAuthError = error instanceof OperatorApiError && (error.status === 401 || error.status === 403);
  const setFilter = (key: keyof PerformanceQuery, value: string) => {
    setLoading(true);
    setError(null);
    setQuery((current) => ({ ...current, [key]: value || undefined }));
  };
  const retry = () => {
    setLoading(true);
    setError(null);
    setRequestRevision((value) => value + 1);
  };

  return (
    <main className="performance-page" id="main-content">
      <header className="performance-hero">
        <div><p className="eyebrow">Long-term performance</p><h1>모델 성능 판독지</h1><p>서버가 확정한 코호트, 표본 수, 평가 정정과 기준선을 같은 화면에서 읽습니다.</p></div>
        <div className="performance-labels"><span className={client.dataMode === "synthetic-test" ? "warning-chip" : "status-chip"}>{client.dataMode === "synthetic-test" ? "합성 API fixture · 실제 성능 아님" : "운영 API"}</span><span className="status-chip">표시 시간 KST (Asia/Seoul)</span><span className="status-chip">원본 시각 UTC</span></div>
      </header>

      <section className="performance-filters" aria-label="성능 코호트 필터">
        <label>남녀<select value={query.division ?? ""} onChange={(event) => setFilter("division", event.target.value)}><option value="">전체</option>{[...options.divisions].sort().map((value) => <option key={value} value={value}>{value === "women" ? "여자" : "남자"}</option>)}</select></label>
        <label>대회<select value={query.competition ?? ""} onChange={(event) => setFilter("competition", event.target.value)}><option value="">전체</option>{[...options.competitions].sort().map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Provider<select value={query.provider ?? ""} onChange={(event) => setFilter("provider", event.target.value)}><option value="">전체</option>{[...options.providers].sort().map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>모델 버전<select value={query.model ?? ""} onChange={(event) => setFilter("model", event.target.value)}><option value="">전체</option>{[...options.models].sort().map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Prompt 버전<select value={query.prompt_version ?? ""} onChange={(event) => setFilter("prompt_version", event.target.value)}><option value="">전체</option>{[...options.prompts].sort().map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Feature 버전<select value={query.feature_version ?? ""} onChange={(event) => setFilter("feature_version", event.target.value)}><option value="">전체</option>{[...options.features].sort().map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>입력 정책<select value={query.availability_policy ?? ""} onChange={(event) => setFilter("availability_policy", event.target.value)}><option value="">전체</option>{[...options.availabilityPolicies].sort().map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>시점 적격성<select value={query.timing_eligibility ?? ""} onChange={(event) => setFilter("timing_eligibility", event.target.value)}><option value="">전체</option>{[...options.timingEligibilities].sort().map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>결과 finality<select value={query.result_finality ?? ""} onChange={(event) => setFilter("result_finality", event.target.value)}><option value="">전체</option>{[...options.resultFinalities].sort().map((value) => <option key={value}>{value}</option>)}</select></label>
      </section>
      {loading && !response && <div className="performance-state" role="status" aria-busy="true"><p>서버 집계를 불러오는 중입니다</p></div>}
      {error && <div role="alert"><StatePanel title={isAuthError ? "운영자 인증을 확인해 주세요" : "성능 데이터를 불러오지 못했습니다"} description={error.message}><button className="primary-button" type="button" onClick={isAuthError ? onSignOut : retry}>{isAuthError ? "인증 다시 입력" : "다시 시도"}</button></StatePanel></div>}
      {!loading && !error && response && rows.length === 0 && <StatePanel title="조건에 맞는 평가가 없습니다" description="서버가 이 코호트에 대한 평가 행을 반환하지 않았습니다. 필터를 변경해 주세요." />}

      {response && rows.length > 0 && !error && (
        <div className={loading ? "performance-content is-refreshing" : "performance-content"} aria-busy={loading}>
          <section className="performance-overview" aria-labelledby="overview-title">
            <div><p className="eyebrow">Server-owned cohort</p><h2 id="overview-title">{primary ? modelLabel(primary) : "선택 코호트"}</h2><p className="cohort-copy">{primary ? Object.entries(primary.cohort).map(([key, value]) => `${key}=${value}`).join(" · ") : "코호트 정보 없음"}</p></div>
            {primary && <><CountSummary row={primary} /><p className={primary.market_availability === "available" ? "status-chip" : "warning-chip"}>{marketLabel(primary)}</p></>}
          </section>
          <section className="performance-section" aria-labelledby="metrics-title"><div className="section-heading"><div><p className="eyebrow">Metrics & baselines</p><h2 id="metrics-title">지표와 기준선</h2></div><p>홈 승률·통계·가용 시장 기준선도 독립된 서버 행으로 표시합니다.</p></div><MetricTable rows={rows} /></section>
          {primary && <><PairedComparison row={primary} /><CalibrationPanel row={primary} /></>}
          <details className="performance-provenance"><summary>평가 버전과 revision 확인</summary><dl><div><dt>응답 스키마</dt><dd className="numeric">{response.metadata.schema_version}</dd></div><div><dt>코호트 정책</dt><dd className="numeric">{response.data.cohort_policy_version}</dd></div><div><dt>평가 revision</dt><dd className="numeric">{primary?.evaluation_revision_ids.join(", ") || "없음"}</dd></div><div><dt>결과 revision</dt><dd className="numeric">{primary?.result_revision_ids.join(", ") || "없음"}</dd></div><div><dt>평가기</dt><dd className="numeric">{primary?.evaluator_version ?? "없음"}</dd></div><div><dt>모델 버전</dt><dd className="numeric">{Object.entries(response.metadata.model_versions).map(([provider, versions]) => `${provider}: ${versions.join(", ")}`).join(" · ") || "없음"}</dd></div></dl><p>정정된 평가는 새 evaluation revision으로 추적합니다. 화면은 서버 집계와 revision을 재계산하지 않습니다.</p></details>
        </div>
      )}
    </main>
  );
}
