import { FormEvent, useEffect, useMemo, useState } from "react";

import { AppNavigation } from "./components/AppNavigation";
import { StatePanel } from "./components/StatePanel";
import { createHistoryApiClient } from "./features/history/api";
import { HistoryPage } from "./features/history/HistoryPage";
import { createOperatorApiClient } from "./features/matches/api";
import { MatchBriefingPage } from "./features/matches/MatchBriefingPage";
import { createOperationsApiClient } from "./features/operations/api";
import { OperationsPage } from "./features/operations/OperationsPage";
import { createPerformanceApiClient } from "./features/performance/api";
import { PerformancePage } from "./features/performance/PerformancePage";

const SESSION_TOKEN_KEY = "vlytics.operator-token";

function readSessionToken(): string {
  try {
    return sessionStorage.getItem(SESSION_TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function normalizedPathname() {
  const path = window.location.pathname.replace(/\/$/, "");
  return path || "/";
}

export function App() {
  const [token, setToken] = useState(readSessionToken);
  const [draftToken, setDraftToken] = useState("");
  const [pathname, setPathname] = useState(normalizedPathname);
  const matchClient = useMemo(() => createOperatorApiClient({ token }), [token]);
  const historyClient = useMemo(() => createHistoryApiClient({ token }), [token]);
  const operationsClient = useMemo(() => createOperationsApiClient({ token }), [token]);
  const performanceClient = useMemo(() => createPerformanceApiClient({ token }), [token]);

  useEffect(() => {
    const onPopState = () => setPathname(normalizedPathname());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  function navigate(path: string) {
    if (normalizedPathname() !== path) window.history.pushState({}, "", path);
    setPathname(path);
  }

  function authenticate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = draftToken.trim();
    if (!normalized) return;
    sessionStorage.setItem(SESSION_TOKEN_KEY, normalized);
    setToken(normalized);
    setDraftToken("");
  }

  function signOut() {
    sessionStorage.removeItem(SESSION_TOKEN_KEY);
    setToken("");
  }

  if (!token) {
    return (
      <main className="auth-shell" id="main-content">
        <a className="brand" href="/">vlytics</a>
        <StatePanel title="운영자 인증이 필요합니다" description="발급받은 운영자 토큰은 현재 브라우저 세션에만 보관됩니다. 번들이나 URL에는 저장하지 않습니다.">
          <form className="auth-form" onSubmit={authenticate}>
            <label htmlFor="operator-token">운영자 토큰</label>
            <input id="operator-token" type="password" autoComplete="off" value={draftToken} onChange={(event) => setDraftToken(event.target.value)} required />
            <button className="primary-button" type="submit">분석 화면 열기</button>
          </form>
        </StatePanel>
      </main>
    );
  }

  if (pathname === "/") return <MatchBriefingPage client={matchClient} onSignOut={signOut} />;
  if (pathname === "/history") return <HistoryPage client={historyClient} onSignOut={signOut} onNavigate={navigate} />;
  if (pathname === "/operations") return <OperationsPage client={operationsClient} onSignOut={signOut} onNavigate={navigate} />;
  if (pathname === "/performance") {
    return (
      <>
        <a className="skip-link" href="#main-content">본문으로 이동</a>
        <AppNavigation currentPath="/performance" onNavigate={navigate} onSignOut={signOut} />
        <PerformancePage client={performanceClient} onSignOut={signOut} />
      </>
    );
  }

  return (
    <>
      <AppNavigation currentPath={pathname} onNavigate={navigate} onSignOut={signOut} />
      <main className="page-shell" id="main-content">
        <StatePanel kind="empty" title="페이지를 찾을 수 없습니다" description="주요 메뉴에서 운영자 화면을 선택해 주세요." />
      </main>
    </>
  );
}
