import { FormEvent, useEffect, useRef, useState } from "react";

import { AppNavigation } from "../../components/AppNavigation";
import { StatePanel } from "../../components/StatePanel";
import { StatusBadge } from "../../components/StatusBadge";
import type { CoverageResponse, OperationSummary, OperationsApiClient, OperationsResponse } from "./types";
import "./operations.css";

type LoadState<T> =
  | { state: "loading" }
  | { state: "error"; message: string; retryable: boolean; status: number | null }
  | { state: "ready"; value: T };

const operationStates = ["", "queued", "running", "retry_wait", "succeeded", "failed", "cancelled", "expired", "quarantined"];

function errorDetails(error: unknown) {
  return {
    message: error instanceof Error ? error.message : "운영 상태를 불러오지 못했습니다.",
    retryable: error instanceof Error && "retryable" in error ? Boolean(error.retryable) : true,
    status: error instanceof Error && "status" in error && typeof error.status === "number" ? error.status : null,
  };
}

function formatTimestamp(value: string | null) {
  if (!value) return "마감 없음";
  return new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Seoul" }).format(new Date(value));
}

function formatMoney(value: string, currency: string) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "산출 불가";
  return new Intl.NumberFormat("ko-KR", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 8,
  }).format(amount);
}

function budgetPeriodLabel(period: "day" | "month") {
  return period === "day" ? "일간" : "월간";
}
function retryAllowed(job: OperationSummary, now: number) {
  if (!job.retryable) return false;
  return !job.deadline_at || new Date(job.deadline_at).getTime() > now;
}

function navigate(url: URL) {
  window.history.pushState({}, "", url);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function createIdempotencyKey(jobId: string) {
  const random = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `session-${jobId}`;
  return `operator-retry:${jobId}:${random}`;
}

export function OperationsPage({ client, onSignOut, onNavigate }: { client: OperationsApiClient; onSignOut?: () => void; onNavigate: (path: string) => void }) {
  const [locationKey, setLocationKey] = useState(() => window.location.href);
  const query = new URL(locationKey).searchParams;
  const stateFilter = query.get("state") ?? "";
  const jobTypeFilter = query.get("job_type") ?? "";
  const [draftState, setDraftState] = useState(stateFilter);
  const [draftJobType, setDraftJobType] = useState(jobTypeFilter);
  const [version, setVersion] = useState(0);
  const [operations, setOperations] = useState<LoadState<OperationsResponse>>({ state: "loading" });
  const [coverage, setCoverage] = useState<LoadState<CoverageResponse>>({ state: "loading" });
  const [retryState, setRetryState] = useState<Record<string, "pending" | "done" | "error">>({});
  const [retryErrors, setRetryErrors] = useState<Record<string, string>>({});
  const [now, setNow] = useState(() => Date.now());
  const retryKeys = useRef(new Map<string, string>());
  const cursor = query.get("cursor") ?? undefined;

  useEffect(() => {
    const onPopState = () => {
      const nextLocation = window.location.href;
      const nextQuery = new URL(nextLocation).searchParams;
      setDraftState(nextQuery.get("state") ?? "");
      setDraftJobType(nextQuery.get("job_type") ?? "");
      setOperations({ state: "loading" });
      setLocationKey(nextLocation);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    client.getOperations({ state: stateFilter || undefined, jobType: jobTypeFilter || undefined, cursor }, controller.signal)
      .then((value) => setOperations({ state: "ready", value }))
      .catch((error: unknown) => { if (!controller.signal.aborted) setOperations({ state: "error", ...errorDetails(error) }); });
    return () => controller.abort();
  }, [client, cursor, jobTypeFilter, locationKey, stateFilter, version]);

  useEffect(() => {
    const controller = new AbortController();
    client.getCoverage(controller.signal)
      .then((value) => setCoverage({ state: "ready", value }))
      .catch((error: unknown) => { if (!controller.signal.aborted) setCoverage({ state: "error", ...errorDetails(error) }); });
    return () => controller.abort();
  }, [client, version]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  function applyFilters(event: FormEvent) {
    event.preventDefault();
    const url = new URL(window.location.href);
    url.searchParams.delete("cursor");
    if (draftState) url.searchParams.set("state", draftState);
    else url.searchParams.delete("state");
    if (draftJobType.trim()) url.searchParams.set("job_type", draftJobType.trim());
    else url.searchParams.delete("job_type");
    navigate(url);
  }

  async function retry(job: OperationSummary) {
    if (retryState[job.id] === "pending" || retryState[job.id] === "done" || !retryAllowed(job, now)) return;
    const key = retryKeys.current.get(job.id) ?? createIdempotencyKey(job.id);
    retryKeys.current.set(job.id, key);
    setRetryState((current) => ({ ...current, [job.id]: "pending" }));
    setRetryErrors((current) => ({ ...current, [job.id]: "" }));
    try {
      await client.retryJob(job.id, key);
      setRetryState((current) => ({ ...current, [job.id]: "done" }));
      setOperations({ state: "loading" });
      setCoverage({ state: "loading" });
      setVersion((current) => current + 1);
    } catch (error: unknown) {
      const details = errorDetails(error);
      setRetryState((current) => ({ ...current, [job.id]: "error" }));
      setRetryErrors((current) => ({ ...current, [job.id]: details.message }));
      if ((details.status === 401 || details.status === 403) && onSignOut) onSignOut();
    }
  }

  return (
    <>
      <a className="skip-link" href="#main-content">본문으로 이동</a>
      <AppNavigation currentPath="/operations" onNavigate={onNavigate} onSignOut={onSignOut} />
      <main className="page-shell operations-page" id="main-content">
        <header className="page-heading"><div><h1>운영 상태</h1><p>동기화 coverage, 실패, 재시도 가능 여부와 마감을 서버 기록 그대로 확인합니다.</p></div><StatusBadge tone={operations.state === "ready" && operations.value.data.budgets.length > 0 ? "neutral" : "warning"}>{operations.state === "loading" ? "비용 집계 조회 중" : operations.state === "ready" && operations.value.data.budgets.length > 0 ? "비용 집계: durable ledger" : "비용 기록 없음"}</StatusBadge></header>

        <section className="operations-section" aria-labelledby="budget-title">
          <div className="section-heading"><div><h2 id="budget-title">Provider 비용</h2><p>예약·정산 ledger의 일/월 집계입니다. 미정산 예약은 effective 금액에 보수적으로 포함됩니다.</p></div></div>
          {operations.state === "loading" ? <p role="status">비용 집계를 불러오는 중입니다.</p> : null}
          {operations.state === "error" ? <div className="inline-state" role="alert"><strong>비용 집계를 불러오지 못했습니다</strong><p>{operations.message}</p></div> : null}
          {operations.state === "ready" && operations.value.data.budgets.length === 0 ? <StatePanel kind="empty" title="비용 기록이 없습니다" description="Provider reservation 또는 settlement 기록이 아직 없습니다." /> : null}
          {operations.state === "ready" && operations.value.data.budgets.length > 0 ? <div className="coverage-grid">{operations.value.data.budgets.map((budget) => <article className="coverage-card" key={`${budget.provider}:${budget.currency}:${budget.period}:${budget.period_start}`}><StatusBadge tone={budget.outstanding_count > 0 || budget.conservative_charge_count > 0 ? "warning" : "neutral"}>{budgetPeriodLabel(budget.period)} · {budget.currency}</StatusBadge><h3>{budget.provider}</h3><strong>실제 {formatMoney(budget.actual_amount, budget.currency)}</strong><p className="numeric">예약 {formatMoney(budget.reserved_amount, budget.currency)} · 유효 {formatMoney(budget.effective_amount, budget.currency)}</p><p>{budget.period_start}–{budget.period_end} · 정산 {budget.settled_count}/{budget.reservation_count} · 미정산 {budget.outstanding_count} · 보수 청구 {budget.conservative_charge_count}</p><p className="numeric">기준 {formatTimestamp(budget.as_of)} KST</p></article>)}</div> : null}
        </section>
        <section className="operations-section" aria-labelledby="coverage-title">
          <h2 id="coverage-title">동기화와 누락</h2>
          {coverage.state === "loading" ? <p role="status">Coverage를 불러오는 중입니다.</p> : null}
          {coverage.state === "error" ? <div className="inline-state" role="alert"><strong>Coverage만 불러오지 못했습니다</strong><p>{coverage.message}</p>{coverage.retryable ? <button type="button" className="secondary-button" onClick={() => setVersion((value) => value + 1)}>다시 시도</button> : null}</div> : null}
          {coverage.state === "ready" && coverage.value.data.items.length === 0 ? <StatePanel kind="empty" title="Coverage 기록이 없습니다" description="누락과 최신 동기화 시각을 판단할 서버 기록이 아직 없습니다." /> : null}
          {coverage.state === "ready" && coverage.value.data.items.length > 0 ? <div className="coverage-grid">{coverage.value.data.items.map((item) => <article className="coverage-card" key={`${item.data_kind}:${item.availability}`}><StatusBadge tone={item.availability === "available" ? "neutral" : "warning"}>{item.availability}</StatusBadge><h3>{item.data_kind}</h3><strong>{item.count.toLocaleString("ko-KR")}건</strong><p className="numeric">최근 동기화 {formatTimestamp(item.latest_observed_at)} KST</p><p>{item.evidence_codes.join(", ") || "근거 코드 없음"}</p></article>)}</div> : null}
        </section>

        <section className="operations-section" aria-labelledby="jobs-title">
          <div className="section-heading"><div><h2 id="jobs-title">작업과 실패</h2><p>재시도 가능 플래그와 deadline을 모두 만족하는 작업만 다시 요청할 수 있습니다.</p></div><form className="operation-filters" onSubmit={applyFilters}><label>상태<select value={draftState} onChange={(event) => setDraftState(event.target.value)}>{operationStates.map((state) => <option key={state} value={state}>{state || "전체"}</option>)}</select></label><label>작업 유형<input value={draftJobType} onChange={(event) => setDraftJobType(event.target.value)} /></label><button className="primary-button" type="submit">필터 적용</button></form></div>
          {operations.state === "loading" ? <StatePanel kind="loading" title="작업 상태를 불러오는 중입니다" description="운영 작업의 현재 상태를 조회하고 있습니다." /> : null}
          {operations.state === "error" ? <StatePanel kind="error" title="작업 상태를 불러오지 못했습니다" description={operations.message}>{operations.status === 401 || operations.status === 403 ? <button type="button" className="primary-button" onClick={onSignOut}>인증 다시 입력</button> : operations.retryable ? <button type="button" className="primary-button" onClick={() => setVersion((value) => value + 1)}>다시 시도</button> : null}</StatePanel> : null}
          {operations.state === "ready" && operations.value.data.items.length === 0 ? <StatePanel kind="empty" title="조건에 맞는 작업이 없습니다" description="선택한 상태와 작업 유형에 해당하는 서버 작업 기록이 없습니다." /> : null}
          {operations.state === "ready" && operations.value.data.items.length > 0 ? <div className="operation-table-wrap"><table className="operation-table"><caption>운영 작업 상태와 재시도 가능 여부</caption><thead><tr><th>작업</th><th>상태</th><th>예정 / 마감</th><th>시도</th><th>오류</th><th>Action</th></tr></thead><tbody>{operations.value.data.items.map((job) => { const allowed = retryAllowed(job, now); const state = retryState[job.id]; return <tr key={job.id}><td><strong>{job.job_type}</strong><small className="numeric">{job.id}</small></td><td><StatusBadge tone={job.state === "failed" || job.state === "expired" || job.state === "quarantined" ? "warning" : "neutral"}>{job.state}</StatusBadge></td><td className="numeric"><span>{formatTimestamp(job.due_at)} KST</span><small>{formatTimestamp(job.deadline_at)}{job.deadline_at ? " KST" : ""}</small></td><td>{job.attempt_no}</td><td>{job.error_code ?? "없음"}</td><td>{allowed ? <button type="button" className="secondary-button" disabled={state === "pending" || state === "done"} onClick={() => void retry(job)}>{state === "pending" ? "요청 중" : state === "done" ? "요청 완료" : "재시도"}</button> : <span className="muted">{job.retryable ? "마감 지남" : "재시도 불가"}</span>}{retryErrors[job.id] ? <small role="alert">{retryErrors[job.id]}</small> : null}</td></tr>; })}</tbody></table></div> : null}
          {operations.state === "ready" && operations.value.data.next_cursor ? <button type="button" className="secondary-button pagination-button" onClick={() => { const url = new URL(window.location.href); url.searchParams.set("cursor", operations.value.data.next_cursor as string); navigate(url); }}>다음 작업</button> : null}
        </section>
        {operations.state === "ready" ? <p className="revision-note numeric">Schema {operations.value.metadata.schema_version} · schedule revisions {operations.value.metadata.schedule_revision_ids.join(", ") || "n0"}</p> : null}
      </main>
    </>
  );
}
