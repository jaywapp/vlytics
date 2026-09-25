import { FormEvent, useEffect, useMemo, useState } from "react";

import { AppNavigation } from "../../components/AppNavigation";
import { StatePanel } from "../../components/StatePanel";
import { StatusBadge } from "../../components/StatusBadge";
import type { Division } from "../matches/types";
import type { HistoryApiClient, HistoryQuery, PredictionHistoryItem, PredictionHistoryResponse } from "./types";
import "./history.css";

type LoadState =
  | { state: "loading" }
  | { state: "error"; message: string; retryable: boolean; status: number | null }
  | { state: "ready"; value: PredictionHistoryResponse };

type Filters = {
  startDate: string;
  endDate: string;
  division: Division | "";
  team: string;
  competition: string;
  provider: string;
  model: string;
  predictionType: string;
  promptVersion: string;
};

const filterKeys = ["start", "end", "division", "team", "competition", "provider", "model", "type", "prompt"] as const;

function filtersFromSearch(search: string): Filters {
  const query = new URLSearchParams(search);
  const division = query.get("division");
  return {
    startDate: query.get("start") ?? "",
    endDate: query.get("end") ?? "",
    division: division === "men" || division === "women" ? division : "",
    team: query.get("team") ?? "",
    competition: query.get("competition") ?? "",
    provider: query.get("provider") ?? "",
    model: query.get("model") ?? "",
    predictionType: query.get("type") ?? "",
    promptVersion: query.get("prompt") ?? "",
  };
}

function nextKstDateBoundary(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  const next = new Date(Date.UTC(year, month - 1, day + 1));
  const nextDate = [next.getUTCFullYear(), next.getUTCMonth() + 1, next.getUTCDate()]
    .map((value, index) => (index === 0 ? String(value) : String(value).padStart(2, "0")))
    .join("-");
  return `${nextDate}T00:00:00+09:00`;
}
function apiQuery(filters: Filters, search: string): HistoryQuery {
  const query = new URLSearchParams(search);
  return {
    division: filters.division || undefined,
    team: filters.team || undefined,
    competition: filters.competition || undefined,
    provider: filters.provider || undefined,
    model: filters.model || undefined,
    predictionType: filters.predictionType || undefined,
    promptVersion: filters.promptVersion || undefined,
    startAt: filters.startDate ? `${filters.startDate}T00:00:00+09:00` : undefined,
    endAt: filters.endDate ? nextKstDateBoundary(filters.endDate) : undefined,
    cursor: query.get("cursor") ?? undefined,
  };
}

function errorDetails(error: unknown) {
  return {
    message: error instanceof Error ? error.message : "예측 기록을 불러오지 못했습니다.",
    retryable: error instanceof Error && "retryable" in error ? Boolean(error.retryable) : true,
    status: error instanceof Error && "status" in error && typeof error.status === "number" ? error.status : null,
  };
}

function formatTimestamp(value: string) {
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Seoul",
  }).format(new Date(value));
}

function safeProbability(output: Record<string, unknown>): string {
  const value = output.home_win_probability;
  return typeof value === "number" && Number.isFinite(value)
    ? new Intl.NumberFormat("ko-KR", { style: "percent", maximumFractionDigits: 1 }).format(value)
    : "기록 없음";
}

function navigate(url: URL, replace = false) {
  window.history[replace ? "replaceState" : "pushState"]({}, "", url);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function HistoryDetail({ item, onClose }: { item: PredictionHistoryItem; onClose: () => void }) {
  const capabilities = Array.isArray(item.output.capabilities)
    ? item.output.capabilities.filter((value): value is string => typeof value === "string")
    : [];
  return (
    <section className="record-card history-detail" aria-labelledby="history-detail-title">
      <button type="button" className="text-button" onClick={onClose}>← 목록으로</button>
      <h2 id="history-detail-title">불변 예측 Snapshot</h2>
      <p>원문 응답이나 인증 정보 없이 서버가 공개한 식별자와 검증된 요약만 표시합니다.</p>
      <dl className="record-list">
        <div><dt>Prediction revision</dt><dd className="numeric">{item.id}</dd></div>
        <div><dt>Match</dt><dd className="numeric">{item.match_id}</dd></div>
        <div><dt>생성 시각</dt><dd className="numeric">{formatTimestamp(item.generated_at)} KST</dd></div>
        <div><dt>Provider / Model</dt><dd>{item.provider} / <span className="numeric">{item.resolved_model_id}</span></dd></div>
        <div><dt>Model / Prompt version</dt><dd className="numeric">{item.model_version ?? "기록 없음"} / {item.prompt_version}</dd></div>
        <div><dt>Feature version</dt><dd className="numeric">{item.feature_version}</dd></div>
        <div><dt>Schedule revision</dt><dd className="numeric">{item.schedule_revision_id}</dd></div>
        <div><dt>Source snapshot</dt><dd className="numeric">{item.source_snapshot_id}</dd></div>
        <div><dt>Evaluation revision</dt><dd className="numeric">{item.evaluation_revision_id ?? "평가 전"}</dd></div>
        <div><dt>홈 승리 확률</dt><dd>{safeProbability(item.output)}</dd></div>
        <div><dt>지원 target</dt><dd>{capabilities.join(", ") || "기록 없음"}</dd></div>
      </dl>
    </section>
  );
}

export function HistoryPage({
  client,
  onSignOut,
  onNavigate,
}: {
  client: HistoryApiClient;
  onSignOut?: () => void;
  onNavigate: (path: string) => void;
}) {
  const [locationKey, setLocationKey] = useState(() => window.location.href);
  const initialFilters = useMemo(() => filtersFromSearch(new URL(locationKey).search), [locationKey]);
  const [draft, setDraft] = useState(initialFilters);
  const [loadVersion, setLoadVersion] = useState(0);
  const [result, setResult] = useState<LoadState>({ state: "loading" });

  useEffect(() => {
    const onPopState = () => {
      const nextLocation = window.location.href;
      setDraft(filtersFromSearch(new URL(nextLocation).search));
      setResult({ state: "loading" });
      setLocationKey(nextLocation);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    client
      .getPredictions(apiQuery(initialFilters, new URL(locationKey).search), controller.signal)
      .then((value) => setResult({ state: "ready", value }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setResult({ state: "error", ...errorDetails(error) });
      });
    return () => controller.abort();
  }, [client, initialFilters, loadVersion, locationKey]);

  const search = new URL(locationKey).search;
  const selectedId = new URLSearchParams(search).get("prediction");
  const selected = result.state === "ready" ? result.value.data.items.find((item) => item.id === selectedId) : undefined;

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const url = new URL(window.location.href);
    filterKeys.forEach((key) => url.searchParams.delete(key));
    url.searchParams.delete("cursor");
    url.searchParams.delete("prediction");
    const entries: Array<[string, string]> = [
      ["start", draft.startDate], ["end", draft.endDate], ["division", draft.division],
      ["team", draft.team.trim()], ["competition", draft.competition.trim()], ["provider", draft.provider.trim()],
      ["model", draft.model.trim()], ["type", draft.predictionType.trim()], ["prompt", draft.promptVersion.trim()],
    ];
    entries.forEach(([key, value]) => value && url.searchParams.set(key, value));
    navigate(url);
  }

  return (
    <>
      <a className="skip-link" href="#main-content">본문으로 이동</a>
      <AppNavigation currentPath="/history" onNavigate={onNavigate} onSignOut={onSignOut} />
      <main className="page-shell history-page" id="main-content">
        <header className="page-heading">
          <div><h1>예측 기록</h1><p>서버가 고정한 Snapshot과 revision을 필터링해 추적합니다.</p></div>
          <StatusBadge>최신 생성 시각순 · 서버 정렬</StatusBadge>
        </header>
        <form className="filter-panel" aria-label="예측 기록 필터" onSubmit={applyFilters}>
          <label>시작일 · KST<input type="date" value={draft.startDate} onChange={(event) => setDraft({ ...draft, startDate: event.target.value })} /></label>
          <label>종료일 · KST<input type="date" value={draft.endDate} onChange={(event) => setDraft({ ...draft, endDate: event.target.value })} /></label>
          <label>구분<select value={draft.division} onChange={(event) => setDraft({ ...draft, division: event.target.value as Division | "" })}><option value="">전체</option><option value="women">여자부</option><option value="men">남자부</option></select></label>
          <label>팀<input value={draft.team} onChange={(event) => setDraft({ ...draft, team: event.target.value })} /></label>
          <label>대회<input value={draft.competition} onChange={(event) => setDraft({ ...draft, competition: event.target.value })} /></label>
          <label>Provider<input value={draft.provider} onChange={(event) => setDraft({ ...draft, provider: event.target.value })} /></label>
          <label>Model<input value={draft.model} onChange={(event) => setDraft({ ...draft, model: event.target.value })} /></label>
          <label>예측 유형<input value={draft.predictionType} onChange={(event) => setDraft({ ...draft, predictionType: event.target.value })} /></label>
          <label>Prompt version<input value={draft.promptVersion} onChange={(event) => setDraft({ ...draft, promptVersion: event.target.value })} /></label>
          <button className="primary-button" type="submit">필터 적용</button>
        </form>
        {result.state === "loading" ? <StatePanel kind="loading" title="기록을 불러오는 중입니다" description="불변 예측 Snapshot을 조회하고 있습니다." /> : null}
        {result.state === "error" ? (
          <StatePanel kind="error" title="예측 기록을 불러오지 못했습니다" description={result.message}>
            {result.status === 401 || result.status === 403 ? <button className="primary-button" type="button" onClick={onSignOut}>인증 다시 입력</button> : result.retryable ? <button className="primary-button" type="button" onClick={() => setLoadVersion((value) => value + 1)}>다시 시도</button> : null}
          </StatePanel>
        ) : null}
        {result.state === "ready" && result.value.data.items.length === 0 ? <StatePanel kind="empty" title="조건에 맞는 예측 기록이 없습니다" description="필터를 완화하면 다른 불변 기록을 확인할 수 있습니다." /> : null}
        {result.state === "ready" && selected ? <HistoryDetail item={selected} onClose={() => window.history.back()} /> : null}
        {result.state === "ready" && !selected ? (
          <>
            <div className="history-list" aria-label="예측 기록 목록">
              {result.value.data.items.map((item) => (
                <article className="record-card" key={item.id}>
                  <div className="record-card__heading"><div><small>{item.competition} · {item.division === "women" ? "여자부" : "남자부"}</small><h2>{item.provider} · {item.prediction_type}</h2></div><StatusBadge tone={item.status === "succeeded" ? "neutral" : "warning"}>{item.status}</StatusBadge></div>
                  <p className="numeric">{formatTimestamp(item.generated_at)} KST</p>
                  <dl className="compact-facts"><div><dt>Model</dt><dd>{item.resolved_model_id}</dd></div><div><dt>Prompt</dt><dd>{item.prompt_version}</dd></div><div><dt>승리 확률</dt><dd>{safeProbability(item.output)}</dd></div></dl>
                  <button type="button" className="secondary-button" onClick={() => { const url = new URL(window.location.href); url.searchParams.set("prediction", item.id); navigate(url); }}>Snapshot 보기</button>
                </article>
              ))}
            </div>
            {result.value.data.next_cursor ? <button className="secondary-button pagination-button" type="button" onClick={() => { const url = new URL(window.location.href); url.searchParams.set("cursor", result.value.data.next_cursor as string); navigate(url); }}>다음 기록</button> : null}
          </>
        ) : null}
        {result.state === "ready" ? <p className="revision-note numeric">Schema {result.value.metadata.schema_version} · prediction revisions {result.value.metadata.prediction_revision_ids.join(", ") || "n0"}</p> : null}
      </main>
    </>
  );
}
