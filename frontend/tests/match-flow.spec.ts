import "@testing-library/jest-dom/vitest";

import { createElement } from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createOperatorApiClient, OperatorApiError } from "../src/features/matches/api";
import { MatchBriefingPage } from "../src/features/matches/MatchBriefingPage";
import { createSyntheticFixtureClient } from "../src/features/matches/fixtures";
import type { OperatorApiClient } from "../src/features/matches/types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderBriefing(client: OperatorApiClient, initialDate = "2026-09-20") {
  return render(createElement(MatchBriefingPage, { client, initialDate }));
}

describe("match briefing flow", () => {
  it("renders the API fixture with explicit synthetic, revision, provider, probability, and time labels", async () => {
    renderBriefing(createSyntheticFixtureClient());

    expect(await screen.findByText(/합성 API fixture/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: /한강 블루웨이브 승리 확률 62%/ })).toBeInTheDocument();

    const comparison = screen.getByRole("table", {
      name: /통계 · GPT · Claude · Gemini/,
    });
    expect(within(comparison).getByText("GPT 독립 실험군")).toBeInTheDocument();
    expect(within(comparison).getByText("Claude 독립 실험군")).toBeInTheDocument();
    expect(within(comparison).getByText("Gemini 독립 실험군")).toBeInTheDocument();
    expect(within(comparison).getByText("시간 초과")).toBeInTheDocument();
    expect(within(comparison).getByText("provider_timeout")).toBeInTheDocument();
    expect(within(comparison).getByText("statistical-synthetic-2026-09")).toBeInTheDocument();
    expect(within(comparison).getAllByText("버전 2026-09").length).toBeGreaterThan(0);

    expect(screen.getByRole("img", { name: /3:0 16%.*0:3 9%/ })).toBeInTheDocument();
    expect(screen.getByText(/검증된 서버 분포 참조/)).toBeInTheDocument();
    expect(screen.getByText("총점 O/U")).toBeInTheDocument();
    expect(screen.getAllByText(/2026\. 9\. 20\./).length).toBeGreaterThanOrEqual(2);

    await userEvent.click(screen.getByText("입력과 예측 기록 확인"));
    expect(screen.getByText("vlytics.operator.v1")).toBeInTheDocument();
    expect(screen.getByText("feature-v2")).toBeInTheDocument();
    expect(screen.getByText("모델 버전").nextElementSibling).toHaveTextContent("2026-09");
  });

  it("supports keyboard match selection and exposes missing market without invented values", async () => {
    const user = userEvent.setup();
    renderBriefing(createSyntheticFixtureClient());

    const longMatch = await screen.findByRole("button", {
      name: /매우 긴 이름을 가진 합성 홈 배구단 테스트 클럽/,
    });
    longMatch.focus();
    await user.keyboard("{Enter}");

    expect(
      await screen.findByRole("heading", {
        name: /매우 긴 이름을 가진 합성 홈 배구단 테스트 클럽 승리 확률 48%/,
      }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("미수신").length).toBeGreaterThan(0);
    expect(screen.getByText("market_source_not_configured_or_no_eligible_quote")).toBeInTheDocument();
    expect(screen.getAllByText("응답 없음")).toHaveLength(2);
    expect(screen.queryByText("provider_outcome_missing")).not.toBeInTheDocument();
  });

  it("shows the server empty state without substituting synthetic matches", async () => {
    renderBriefing(createSyntheticFixtureClient({ empty: true }), "2030-01-01");
    expect(await screen.findByRole("heading", { name: "표시할 경기가 없습니다" })).toBeInTheDocument();
    expect(screen.queryByText("한강 블루웨이브")).not.toBeInTheDocument();
  });

  it("keeps schedule and match errors actionable and separate", async () => {
    const view = renderBriefing(
      createSyntheticFixtureClient({ scheduleError: new Error("schedule unavailable") }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("schedule unavailable");

    view.rerender(
      createElement(MatchBriefingPage, {
        client: createSyntheticFixtureClient({ detailError: new Error("detail unavailable") }),
        initialDate: "2026-09-20",
      }),
    );
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("detail unavailable"));
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeEnabled();
  });


  it("does not fill unsupported provider targets from another model", async () => {
    const user = userEvent.setup();
    renderBriefing(createSyntheticFixtureClient());

    const comparison = await screen.findByRole("table", { name: /통계 · GPT · Claude · Gemini/ });
    const geminiRow = within(comparison).getByRole("row", { name: /Gemini 독립 실험군/ });
    await user.click(within(geminiRow).getByRole("button", { name: "분석 보기" }));

    expect(
      screen.getByRole("heading", { name: /한강 블루웨이브 승리 확률 57%/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("세트 분포 미지원")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /세트 스코어 확률/ })).not.toBeInTheDocument();
  });

  it("offers credential replacement instead of retry for authentication failures", async () => {
    const onSignOut = vi.fn();
    const client: OperatorApiClient = {
      dataMode: "live",
      getSchedule: () =>
        Promise.reject(
          new OperatorApiError(401, {
            code: "invalid_credentials",
            message: "인증 정보를 다시 확인해 주세요.",
            retryable: false,
            correlation_id: "synthetic-correlation",
          }),
        ),
      getMatch: () => new Promise(() => undefined),
    };
    render(
      createElement(MatchBriefingPage, {
        client,
        initialDate: "2026-09-20",
        onSignOut,
      }),
    );

    const reset = await screen.findByRole("button", { name: "인증 다시 입력" });
    expect(screen.queryByRole("button", { name: "다시 시도" })).not.toBeInTheDocument();
    await userEvent.click(reset);
    expect(onSignOut).toHaveBeenCalledOnce();
  });
  it("announces loading while the schedule request is pending", () => {
    const pendingClient: OperatorApiClient = {
      dataMode: "synthetic-test",
      getSchedule: () => new Promise(() => undefined),
      getMatch: () => new Promise(() => undefined),
    };
    renderBriefing(pendingClient);
    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("경기 일정을 불러오는 중입니다")).toBeInTheDocument();
  });
});


describe("operator API client", () => {
  it("sends operator authentication and the documented KST schedule query", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ metadata: {}, data: {} }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = createOperatorApiClient({ token: "fixture-token" });

    await client.getSchedule({ date: "2026-09-20", timezone: "Asia/Seoul", division: "women" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/schedule?date=2026-09-20&timezone=Asia%2FSeoul&limit=100&division=women",
      {
        headers: { Authorization: "Bearer fixture-token" },
        signal: undefined,
      },
    );
  });

  it("parses a forbidden operator response without putting the token in the URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: () =>
        Promise.resolve({
          code: "operator_role_required",
          message: "operator role required",
          retryable: false,
          correlation_id: "synthetic-correlation",
        }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = createOperatorApiClient({ token: "fixture-token" });

    await expect(
      client.getSchedule({ date: "2026-09-20", timezone: "Asia/Seoul" }),
    ).rejects.toMatchObject({
      status: 403,
      code: "operator_role_required",
      retryable: false,
    });
    expect(fetchMock.mock.calls[0]?.[0]).not.toContain("fixture-token");
  });
});
