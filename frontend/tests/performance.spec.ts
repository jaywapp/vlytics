import "@testing-library/jest-dom/vitest";

import { createElement } from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OperatorApiError } from "../src/features/matches/api";
import { createPerformanceApiClient } from "../src/features/performance/api";
import { createSyntheticPerformanceClient } from "../src/features/performance/fixtures";
import { PerformancePage } from "../src/features/performance/PerformancePage";
import type { PerformanceApiClient } from "../src/features/performance/types";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("performance dashboard", () => {
  it("shows server metrics, baselines, paired sample mismatch, correction revision, and accessible calibration values", async () => {
    render(createElement(PerformancePage, { client: createSyntheticPerformanceClient() }));

    expect(await screen.findByText(/합성 API fixture/)).toBeInTheDocument();
    const metrics = screen.getByRole("table", { name: /서버 집계 성능 지표/ });
    expect(within(metrics).getByText("70.0%")).toBeInTheDocument();
    expect(within(metrics).getByText("홈 승률 기준선")).toBeInTheDocument();
    expect(within(metrics).getByText("통계 기준선")).toBeInTheDocument();
    const marketRow = within(metrics).getByRole("row", { name: /가용 시장 기준선/ });
    expect(within(marketRow).getByText("시장 데이터 미수신")).toBeInTheDocument();
    expect(screen.getByText("개별 n 20 · 최대 짝비교 n 18")).toBeInTheDocument();
    expect(screen.getByText(/match·schedule·snapshot·cutoff·result revision/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /0–20% 예측 14.0%, 관측 10.0%, n 4/ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /Calibration 차트의 접근 가능한 수치 표/ })).toBeInTheDocument();

    await userEvent.click(screen.getByText("평가 버전과 revision 확인"));
    expect(screen.getByText(/evaluation-r2-corrected/)).toBeInTheDocument();
    expect(screen.getAllByText(/gpt-5.6-sol-2026-10/).length).toBeGreaterThan(0);
  });

  it("preserves n0 and n1 semantics without zero-filling unavailable metrics or confidence intervals", async () => {
    const user = userEvent.setup();
    render(createElement(PerformancePage, { client: createSyntheticPerformanceClient() }));
    await screen.findByText(/합성 API fixture/);

    await user.selectOptions(screen.getByLabelText("모델 버전"), "claude-synthetic-empty");
    await waitFor(() => expect(screen.getByText("평가 n", { selector: "dt" }).nextElementSibling).toHaveTextContent("0"));
    expect(screen.getAllByText("산출 불가").length).toBeGreaterThan(3);
    expect(screen.getAllByText("시장 데이터 미수신").length).toBeGreaterThan(0);

    await user.selectOptions(screen.getByLabelText("모델 버전"), "gpt-5.6-sol-2026-10");
    await waitFor(() => expect(screen.getByText("평가 n", { selector: "dt" }).nextElementSibling).toHaveTextContent("1"));
    expect(screen.getByText(/짝비교 n이 2보다 작은/)).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: /동일 경기 기준선/ })).getAllByText("산출 불가").length).toBeGreaterThanOrEqual(2);
  });

  it("sends filter changes back to the server client and does not reaggregate returned rows", async () => {
    const fixture = createSyntheticPerformanceClient();
    const getPerformance = vi.fn(fixture.getPerformance);
    const client: PerformanceApiClient = { dataMode: "synthetic-test", getPerformance };
    const user = userEvent.setup();
    render(createElement(PerformancePage, { client }));
    await screen.findByText(/합성 API fixture/);

    await user.selectOptions(screen.getByLabelText("남녀"), "men");
    await waitFor(() => expect(getPerformance).toHaveBeenLastCalledWith({ division: "men" }, expect.any(AbortSignal)));
    expect(await screen.findByRole("row", { name: /openai · gpt-5.6-sol-2026-09.*8.*1.*시장 데이터 검증 전/ })).toBeInTheDocument();
  });

  it("offers credential replacement for 401 and 403 responses", async () => {
    const onSignOut = vi.fn();
    const client: PerformanceApiClient = {
      dataMode: "live",
      getPerformance: () => Promise.reject(new OperatorApiError(403, {
        code: "operator_role_required",
        message: "운영자 권한이 필요합니다.",
        retryable: false,
        correlation_id: "synthetic-performance-correlation",
      })),
    };
    render(createElement(PerformancePage, { client, onSignOut }));
    const reset = await screen.findByRole("button", { name: "인증 다시 입력" });
    expect(screen.queryByRole("button", { name: "다시 시도" })).not.toBeInTheDocument();
    await userEvent.click(reset);
    expect(onSignOut).toHaveBeenCalledOnce();
  });

  it("renders empty, partial, and retryable error states explicitly", async () => {
    const view = render(createElement(PerformancePage, { client: createSyntheticPerformanceClient({ empty: true }) }));
    expect(await screen.findByRole("heading", { name: "조건에 맞는 평가가 없습니다" })).toBeInTheDocument();

    view.rerender(createElement(PerformancePage, { client: createSyntheticPerformanceClient({ error: new Error("synthetic outage") }) }));
    expect(await screen.findByRole("alert")).toHaveTextContent("synthetic outage");
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeEnabled();
  });
});

describe("performance API client", () => {
  it("uses bearer auth and documented server-side filters without exposing the token", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({ metadata: {}, data: { cohort_policy_version: "v1", items: [] } }) });
    vi.stubGlobal("fetch", fetchMock);
    const client = createPerformanceApiClient({ token: "fixture-token" });
    await client.getPerformance({ division: "women", competition: "regular-2026", model: "model-v2" });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/performance?division=women&competition=regular-2026&model=model-v2", { headers: { Authorization: "Bearer fixture-token" }, signal: undefined });
    expect(fetchMock.mock.calls[0]?.[0]).not.toContain("fixture-token");
  });
});
