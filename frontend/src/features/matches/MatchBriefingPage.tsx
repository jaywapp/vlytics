import { useEffect, useMemo, useState } from "react";

import { ProbabilityChart } from "../../components/ProbabilityChart";
import { ProviderComparison } from "../../components/ProviderComparison";
import { StatePanel } from "../../components/StatePanel";
import { StatusBadge } from "../../components/StatusBadge";
import {
  availabilityLabel,
  coverageSummary,
  matchStateLabel,
  predictionView,
  providerLabel,
  providerStateLabel,
} from "./model";
import type {
  Division,
  MatchDetailResponse,
  MatchSummary,
  OperatorApiClient,
  ProviderOutcome,
  ScheduleResponse,
} from "./types";

type LoadState<T> =
  | { state: "loading" }
  | { state: "error"; message: string; retryable: boolean; status: number | null }
  | { state: "ready"; value: T };

type MatchBriefingPageProps = {
  client: OperatorApiClient;
  onSignOut?: () => void;
  initialDate?: string;
};

const percent = new Intl.NumberFormat("ko-KR", {
  style: "percent",
  maximumFractionDigits: 1,
});

function todayInKst(): string {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul" }).format(new Date());
}

function errorDetails(error: unknown): { message: string; retryable: boolean; status: number | null } {
  if (error instanceof Error) {
    const retryable =
      "retryable" in error && typeof error.retryable === "boolean" ? error.retryable : true;
    const status = "status" in error && typeof error.status === "number" ? error.status : null;
    return { message: error.message, retryable, status };
  }
  return { message: "알 수 없는 오류가 발생했습니다.", retryable: true, status: null };
}

function isAuthenticationError(status: number | null): boolean {
  return status === 401 || status === 403;
}

function formatTimestamp(value: string | null, timeZone: "Asia/Seoul" | "UTC"): string {
  if (!value) return "기록 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone,
  }).format(date);
}

function providerRank(provider: string): number {
  const order = ["statistical", "statistics", "stat", "openai", "anthropic", "google", "gemini"];
  const index = order.indexOf(provider.toLowerCase());
  return index < 0 ? order.length : index;
}

function completeProviderRows(outcomes: ProviderOutcome[]): ProviderOutcome[] {
  const aliases = [
    { key: "statistical", names: ["statistical", "statistics", "stat"] },
    { key: "openai", names: ["openai"] },
    { key: "anthropic", names: ["anthropic"] },
    { key: "google", names: ["google", "gemini"] },
  ];
  const known = new Set<string>();
  const complete = aliases.map(({ key, names }) => {
    const found = outcomes.find((outcome) => names.includes(outcome.provider.toLowerCase()));
    if (found) {
      known.add(found.provider);
      return found;
    }
    return {
      provider: key,
      status: "missing" as const,
      requested_model: null,
      resolved_model_id: null,
      model_version: null,
      prompt_version: null,
      generated_at: null,
      prediction_revision_id: null,
      output: null,
      error_code: null,
    };
  });
  return [...complete, ...outcomes.filter((outcome) => !known.has(outcome.provider))].sort(
    (left, right) => providerRank(left.provider) - providerRank(right.provider),
  );
}

function preferredProvider(outcomes: ProviderOutcome[]): ProviderOutcome | undefined {
  return outcomes.find((outcome) => outcome.status === "succeeded" && outcome.output);
}

function MatchRail({
  matches,
  selectedId,
  onSelect,
}: {
  matches: MatchSummary[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  return (
    <nav className="match-rail" aria-label="경기 선택">
      {matches.map((match) => (
        <button
          type="button"
          key={match.id}
          className={match.id === selectedId ? "match-button match-button--active" : "match-button"}
          aria-pressed={match.id === selectedId}
          onClick={() => onSelect(match.id)}
        >
          <small>
            {match.division === "women" ? "여자부" : "남자부"} ·{" "}
            <span className="numeric">{formatTimestamp(match.scheduled_start_at_local, "Asia/Seoul")}</span>
          </small>
          <span>{match.home_team.name}</span>
          <span className="match-versus">vs</span>
          <span>{match.away_team.name}</span>
          <em>{matchStateLabel(match.status)}</em>
        </button>
      ))}
    </nav>
  );
}

function MatchAnalysis({
  response,
  selectedProvider,
  onProviderChange,
}: {
  response: MatchDetailResponse;
  selectedProvider: string;
  onProviderChange: (provider: string) => void;
}) {
  const match = response.data;
  const outcomes = useMemo(() => completeProviderRows(match.provider_outcomes), [match.provider_outcomes]);
  const active =
    outcomes.find((outcome) => outcome.provider === selectedProvider && outcome.status === "succeeded") ??
    preferredProvider(outcomes);
  const view = predictionView(active?.output ?? null);
  const failedCount = outcomes.filter((outcome) => outcome.status !== "succeeded").length;
  const title =
    view.homeWinProbability === null
      ? `${match.home_team.name}와 ${match.away_team.name}의 예측값을 확인할 수 없습니다.`
      : `${match.home_team.name} 승리 확률 ${percent.format(view.homeWinProbability)}, 불확실성도 함께 기록합니다.`;

  return (
    <article className="brief-content">
      <header className="brief-lead">
        <div className="badge-row">
          <StatusBadge>{matchStateLabel(match.status)}</StatusBadge>
          <StatusBadge tone={failedCount > 0 ? "warning" : "neutral"}>
            {failedCount > 0 ? `Provider ${failedCount}개 미완료` : "모든 Provider 완료"}
          </StatusBadge>
          <StatusBadge tone={match.market.availability === "available" ? "neutral" : "warning"}>
            Market {availabilityLabel(match.market.availability)}
          </StatusBadge>
        </div>
        <h1>{title}</h1>
        <p>
          선택한 <strong>{active ? providerLabel(active.provider) : "모델 없음"}</strong>의 서버 저장 결과입니다.
          실패하거나 지원하지 않는 값은 다른 모델의 값으로 채우지 않습니다.
        </p>
      </header>

      <section className="brief-section" aria-labelledby="provider-comparison-title">
        <h2 id="provider-comparison-title">같은 입력, 모델별 판단</h2>
        <ProviderComparison
          outcomes={outcomes}
          selectedProvider={active?.provider ?? selectedProvider}
          onSelect={onProviderChange}
        />
      </section>

      <div className="brief-columns">
        <section aria-labelledby="set-distribution-title">
          <h2 id="set-distribution-title">여섯 가지 세트 결과</h2>
          {view.setScores ? (
            <ProbabilityChart probabilities={view.setScores} homeTeam={match.home_team.name} />
          ) : (
            <div className="inline-state">
              <strong>세트 분포 {view.capabilities.includes("set_score") ? "누락" : "미지원"}</strong>
              <p>서버 응답에 검증된 여섯 결과가 없어 분포를 그리지 않습니다.</p>
            </div>
          )}
        </section>

        <section aria-labelledby="reason-title">
          <h2 id="reason-title">근거와 위험</h2>
          {view.rationale ? <p>{view.rationale}</p> : <p className="muted">저장된 근거 설명이 없습니다.</p>}
          <h3>남은 불확실성</h3>
          {view.risks.length > 0 ? (
            <ul className="reason-list">
              {view.risks.map((risk) => (
                <li key={risk}>{risk}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">별도 위험 요인이 기록되지 않았습니다.</p>
          )}
          {view.confidenceNote ? <p className="notice">{view.confidenceNote}</p> : null}
        </section>
      </div>

      <section className="brief-section" aria-labelledby="points-title">
        <h2 id="points-title">공동 점수 분포와 포인트 대상</h2>
        <div className="capability-grid">
          <div>
            <span className="field-label">공동 분포</span>
            <strong>{availabilityLabel(view.pointsAvailability)}</strong>
            <p>
              {view.jointDistributionRef
                ? "검증된 서버 분포 참조가 연결되었습니다."
                : "분포 참조가 없어 클라이언트에서 점수 확률을 추정하지 않습니다."}
            </p>
          </div>
          <div>
            <span className="field-label">점수 핸디캡</span>
            <strong>{view.capabilities.includes("point_handicap") ? "지원" : "미지원"}</strong>
            <p>라인별 확률은 해당 Market 라인이 서버에서 제공될 때만 표시합니다.</p>
          </div>
          <div>
            <span className="field-label">총점 O/U</span>
            <strong>{view.capabilities.includes("point_total") ? "지원" : "미지원"}</strong>
            <p>단위는 양 팀의 경기 전체 득점 합계입니다.</p>
          </div>
        </div>
        {view.jointDistributionRef ? (
          <p className="reference numeric">분포 참조: {view.jointDistributionRef}</p>
        ) : null}
      </section>

      <section className="brief-section" aria-labelledby="market-title">
        <h2 id="market-title">Market 비교</h2>
        <div className="market-panel">
          <StatusBadge tone={match.market.availability === "available" ? "neutral" : "warning"}>
            {availabilityLabel(match.market.availability)}
          </StatusBadge>
          <dl className="facts">
            <div>
              <dt>소스</dt>
              <dd>{match.market.source ?? "기록 없음"}</dd>
            </div>
            <div>
              <dt>Quote 기준 시각</dt>
              <dd className="numeric">{formatTimestamp(match.market.quoted_at, "Asia/Seoul")} KST</dd>
            </div>
            <div>
              <dt>Snapshot</dt>
              <dd className="numeric">{match.market.snapshot_id ?? "기록 없음"}</dd>
            </div>
            <div>
              <dt>상태 근거</dt>
              <dd>{match.market.reason ?? "별도 사유 없음"}</dd>
            </div>
          </dl>
          <p className="muted">
            Market은 독립 예측 입력에 포함되지 않습니다. 미수신 또는 미지원 상태에서도 예측 기록은 보존됩니다.
          </p>
        </div>
      </section>

      <section className="brief-section" aria-labelledby="timing-title">
        <h2 id="timing-title">기준 시각과 Snapshot</h2>
        <div className="timeline">
          <div>
            <b>경기 예정</b>
            <p className="numeric">{formatTimestamp(match.scheduled_start_at_local, "Asia/Seoul")} KST</p>
            <p className="numeric">{formatTimestamp(match.scheduled_start_at_utc, "UTC")} UTC</p>
          </div>
          <div>
            <b>선택 예측 저장</b>
            <p className="numeric">{formatTimestamp(active?.generated_at ?? null, "Asia/Seoul")} KST</p>
            <p>{active ? providerStateLabel(active) : "예측 없음"}</p>
          </div>
          <div>
            <b>Feature Snapshot</b>
            <p className="numeric">{match.feature_snapshot_id ?? "생성 전"}</p>
            <p>{match.feature_version ?? "버전 기록 없음"}</p>
          </div>
        </div>
      </section>

      <details className="record-details">
        <summary>입력과 예측 기록 확인</summary>
        <dl className="record-list">
          <div>
            <dt>Schema</dt>
            <dd className="numeric">{response.metadata.schema_version}</dd>
          </div>
          <div>
            <dt>선택 예측 Revision</dt>
            <dd className="numeric">{active?.prediction_revision_id ?? "기록 없음"}</dd>
          </div>
          <div>
            <dt>실행 모델 / Prompt</dt>
            <dd className="numeric">
              {active?.resolved_model_id ?? active?.requested_model ?? "기록 없음"} /{" "}
              {active?.prompt_version ?? "기록 없음"}
            </dd>
          </div>
          <div>
            <dt>모델 버전</dt>
            <dd className="numeric">
              {active?.model_version ?? active?.output?.model_version ?? "기록 없음"}
            </dd>
          </div>
          <div>
            <dt>Source Snapshot</dt>
            <dd className="numeric">
              {response.metadata.source_snapshot_ids.join(", ") || "기록 없음"}
            </dd>
          </div>
          <div>
            <dt>Schedule Revision</dt>
            <dd className="numeric">
              {response.metadata.schedule_revision_ids.join(", ") || "기록 없음"}
            </dd>
          </div>
          <div>
            <dt>Source coverage</dt>
            <dd>{coverageSummary(match)}</dd>
          </div>
        </dl>
      </details>
    </article>
  );
}

export function MatchBriefingPage({ client, onSignOut, initialDate }: MatchBriefingPageProps) {
  const [date, setDate] = useState(initialDate ?? todayInKst);
  const [division, setDivision] = useState<Division | "all">("all");
  const [scheduleVersion, setScheduleVersion] = useState(0);
  const [detailVersion, setDetailVersion] = useState(0);
  const [selectedMatchId, setSelectedMatchId] = useState("");
  const [selectedProvider, setSelectedProvider] = useState("");
  const [schedule, setSchedule] = useState<LoadState<ScheduleResponse>>({ state: "loading" });
  const [detail, setDetail] = useState<LoadState<MatchDetailResponse>>({ state: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    client
      .getSchedule(
        {
          date,
          timezone: "Asia/Seoul",
          division: division === "all" ? undefined : division,
        },
        controller.signal,
      )
      .then((value) => setSchedule({ state: "ready", value }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setSchedule({ state: "error", ...errorDetails(error) });
      });
    return () => controller.abort();
  }, [client, date, division, scheduleVersion]);

  const matches = schedule.state === "ready" ? schedule.value.data.items : [];
  const effectiveMatchId = matches.some((match) => match.id === selectedMatchId)
    ? selectedMatchId
    : (matches[0]?.id ?? "");

  useEffect(() => {
    if (!effectiveMatchId) return;
    const controller = new AbortController();
    client
      .getMatch(effectiveMatchId, controller.signal)
      .then((value) => setDetail({ state: "ready", value }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setDetail({ state: "error", ...errorDetails(error) });
      });
    return () => controller.abort();
  }, [client, effectiveMatchId, detailVersion]);

  const currentDetail =
    detail.state === "ready" && detail.value.data.id !== effectiveMatchId
      ? ({ state: "loading" } as const)
      : detail;

  return (
    <>
      <a className="skip-link" href="#main-content">
        본문으로 이동
      </a>
      {client.dataMode === "synthetic-test" ? (
        <div className="data-banner">
          합성 API fixture · 화면과 계약 검증용이며 실제 일정이나 예측 성능이 아닙니다.
        </div>
      ) : null}
      <header className="topbar">
        <a className="brand" href="/" aria-label="Vlytics 홈">
          vlytics
        </a>
        <nav aria-label="주요 메뉴">
          <a href="/" aria-current="page">
            경기 브리핑
          </a>
          <a href="/history">예측 기록</a>
          <a href="/performance">성능</a>
          <a href="/operations">운영</a>
        </nav>
        {onSignOut ? (
          <button type="button" className="secondary-button" onClick={onSignOut}>
            세션 종료
          </button>
        ) : null}
      </header>

      <main className="page-shell" id="main-content">
        <header className="page-heading">
          <div>
            <h1>경기 브리핑</h1>
            <p>한 경기의 확률에서 근거, 위험, 고정된 입력 기록까지 순서대로 확인합니다.</p>
          </div>
          <div className="filters" aria-label="일정 필터">
            <label htmlFor="schedule-date">경기일 · KST</label>
            <input
              id="schedule-date"
              type="date"
              value={date}
              onChange={(event) => {
                setSchedule({ state: "loading" });
                setDate(event.target.value);
              }}
            />
            <label htmlFor="division">구분</label>
            <select
              id="division"
              value={division}
              onChange={(event) => {
                setSchedule({ state: "loading" });
                setDivision(event.target.value as Division | "all");
              }}
            >
              <option value="all">전체</option>
              <option value="women">여자부</option>
              <option value="men">남자부</option>
            </select>
          </div>
        </header>

        {schedule.state === "loading" ? (
          <StatePanel
            kind="loading"
            title="경기 일정을 불러오는 중입니다"
            description="선택한 KST 경기일의 운영 API 응답을 기다리고 있습니다."
          />
        ) : schedule.state === "error" ? (
          <StatePanel
            kind="error"
            title="경기 일정을 불러오지 못했습니다"
            description={schedule.message}
          >
            {isAuthenticationError(schedule.status) && onSignOut ? (
              <button type="button" className="primary-button" onClick={onSignOut}>
                인증 다시 입력
              </button>
            ) : schedule.retryable ? (
              <button
                type="button"
                className="primary-button"
                onClick={() => {
                  setSchedule({ state: "loading" });
                  setScheduleVersion((version) => version + 1);
                }}
              >
                다시 시도
              </button>
            ) : null}
          </StatePanel>
        ) : matches.length === 0 ? (
          <StatePanel
            kind="empty"
            title="표시할 경기가 없습니다"
            description="선택한 날짜와 구분에 해당하는 경기 기록이 없습니다. 합성 값으로 빈 화면을 채우지 않습니다."
          />
        ) : (
          <div className="brief-layout">
            <MatchRail
              matches={matches}
              selectedId={effectiveMatchId}
              onSelect={(matchId) => {
                setDetail({ state: "loading" });
                setSelectedMatchId(matchId);
              }}
            />
            {currentDetail.state === "loading" ? (
              <StatePanel
                kind="loading"
                title="경기 분석을 불러오는 중입니다"
                description="예측과 Snapshot을 같은 API 응답에서 확인하고 있습니다."
              />
            ) : currentDetail.state === "error" ? (
              <StatePanel kind="error" title="경기 분석을 불러오지 못했습니다" description={currentDetail.message}>
                {isAuthenticationError(currentDetail.status) && onSignOut ? (
                  <button type="button" className="primary-button" onClick={onSignOut}>
                    인증 다시 입력
                  </button>
                ) : currentDetail.retryable ? (
                  <button
                    type="button"
                    className="primary-button"
                    onClick={() => {
                      setDetail({ state: "loading" });
                      setDetailVersion((version) => version + 1);
                    }}
                  >
                    다시 시도
                  </button>
                ) : null}
              </StatePanel>
            ) : (
              <MatchAnalysis
                response={currentDetail.value}
                selectedProvider={selectedProvider}
                onProviderChange={setSelectedProvider}
              />
            )}
          </div>
        )}
      </main>
    </>
  );
}
