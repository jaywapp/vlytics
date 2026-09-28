import "@testing-library/jest-dom/vitest";

import { createElement } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createOperatorApiClient, OperatorApiError } from "../src/features/matches/api";
import { MatchBriefingPage } from "../src/features/matches/MatchBriefingPage";
import { createSyntheticFixtureClient } from "../src/features/matches/fixtures";
import type { MatchDetailResponse, OperatorApiClient, ScheduleResponse } from "../src/features/matches/types";
import serializedApiFixtureJson from "./fixtures/operator-api.serializer.json";

const serializedApiFixture = serializedApiFixtureJson as unknown as {
  schedule: ScheduleResponse;
  match: MatchDetailResponse;
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderBriefing(client: OperatorApiClient, initialDate = "2026-09-20") {
  return render(createElement(MatchBriefingPage, { client, initialDate }));
}

function schedulePage(items: ScheduleResponse["data"]["items"], nextCursor: string | null): ScheduleResponse {
  return {
    metadata: serializedApiFixture.schedule.metadata,
    data: {
      ...serializedApiFixture.schedule.data,
      items,
      next_cursor: nextCursor,
    },
  };
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
    expect(screen.getByText("Variant 모델 버전").nextElementSibling).toHaveTextContent("2026-09");
  });

  it("renders the shared fixture emitted by the FastAPI serializer", async () => {
    const client: OperatorApiClient = {
      dataMode: "synthetic-test",
      getSchedule: () => Promise.resolve(serializedApiFixture.schedule),
      getMatch: () => Promise.resolve(serializedApiFixture.match),
    };
    renderBriefing(client);

    expect(await screen.findByRole("heading", { name: /Home Club 승리 확률 60%/ })).toBeInTheDocument();
    expect(screen.getByText("market-match-1")).toBeInTheDocument();
    expect(screen.getByText("feature-match-1")).toBeInTheDocument();
    expect(screen.getByText("이 시각까지 고정된 입력만 사용")).toBeInTheDocument();
  });

  it("keeps the selected match detail while its active button is clicked again", async () => {
    const user = userEvent.setup();
    let resolveDetail!: (value: MatchDetailResponse) => void;
    const getMatch = vi.fn(
      () =>
        new Promise<MatchDetailResponse>((resolve) => {
          resolveDetail = resolve;
        }),
    );
    const client: OperatorApiClient = {
      dataMode: "synthetic-test",
      getSchedule: () => Promise.resolve(serializedApiFixture.schedule),
      getMatch,
    };
    renderBriefing(client);

    const selectedMatch = (await screen.findAllByRole("button", { name: /Home Club/ })).find(
      (button) => button.getAttribute("aria-pressed") === "true",
    );
    if (!selectedMatch) throw new Error("the first schedule match must be selected");
    expect(selectedMatch).toHaveAttribute("aria-pressed", "true");
    await user.click(selectedMatch);
    expect(getMatch).toHaveBeenCalledOnce();

    act(() => {
      resolveDetail(serializedApiFixture.match);
    });
    const heading = await screen.findByRole("heading", { name: /Home Club 승리 확률 60%/ });
    await user.click(selectedMatch);

    expect(heading).toBeVisible();
    expect(screen.queryByText("경기 분석을 불러오는 중입니다")).not.toBeInTheDocument();
    expect(getMatch).toHaveBeenCalledOnce();
  });

  it("ignores a previous match detail that resolves after a new selection", async () => {
    const firstMatch = serializedApiFixture.schedule.data.items[0];
    const secondMatch = {
      ...firstMatch,
      id: "new-match",
      home_team: { ...firstMatch.home_team, id: "new-home", name: "New Home Club" },
      away_team: { ...firstMatch.away_team, id: "new-away", name: "New Away Club" },
    };
    const secondDetail: MatchDetailResponse = {
      ...serializedApiFixture.match,
      data: {
        ...serializedApiFixture.match.data,
        id: secondMatch.id,
        home_team: secondMatch.home_team,
        away_team: secondMatch.away_team,
      },
    };
    let resolveFirst!: (value: MatchDetailResponse) => void;
    const getMatch = vi.fn<OperatorApiClient["getMatch"]>((matchId) =>
      matchId === firstMatch.id
        ? new Promise<MatchDetailResponse>((resolve) => {
            resolveFirst = resolve;
          })
        : Promise.resolve(secondDetail),
    );
    const client: OperatorApiClient = {
      dataMode: "synthetic-test",
      getSchedule: () => Promise.resolve(schedulePage([firstMatch, secondMatch], null)),
      getMatch,
    };
    const user = userEvent.setup();
    renderBriefing(client);

    await user.click(await screen.findByRole("button", { name: /New Home Club/ }));
    expect(await screen.findByRole("heading", { name: /New Home Club 승리 확률 60%/ })).toBeInTheDocument();

    await act(async () => {
      resolveFirst(serializedApiFixture.match);
      await Promise.resolve();
    });
    expect(screen.getByRole("heading", { name: /New Home Club 승리 확률 60%/ })).toBeInTheDocument();
    expect(screen.queryByText("경기 분석을 불러오는 중입니다")).not.toBeInTheDocument();
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
    expect(screen.getByText("prediction_market_provenance_unavailable")).toBeInTheDocument();
    expect(screen.getAllByText("응답 없음")).toHaveLength(2);
    expect(screen.queryByText("provider_outcome_missing")).not.toBeInTheDocument();
  });

  it("loads every schedule cursor page without dropping the active match", async () => {
    const firstMatch = serializedApiFixture.schedule.data.items[0];
    const secondMatch = {
      ...firstMatch,
      id: "match-page-2",
      home_team: { ...firstMatch.home_team, id: "team-page-2-home", name: "Second Home Club" },
      away_team: { ...firstMatch.away_team, id: "team-page-2-away", name: "Second Away Club" },
    };
    const getSchedule = vi.fn<OperatorApiClient["getSchedule"]>((query) =>
      Promise.resolve(query.cursor ? schedulePage([secondMatch], null) : schedulePage([firstMatch], "schedule-page-2")),
    );
    const client: OperatorApiClient = {
      dataMode: "synthetic-test",
      getSchedule,
      getMatch: () => Promise.resolve(serializedApiFixture.match),
    };
    const user = userEvent.setup();
    renderBriefing(client);

    expect(await screen.findByRole("heading", { name: /Home Club 승리 확률 60%/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "다음 경기 불러오기" }));

    expect(await screen.findByRole("button", { name: /Second Home Club/ })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Home Club/ }).find((button) => button.getAttribute("aria-pressed") === "true")).toBeDefined();
    expect(screen.queryByRole("button", { name: "다음 경기 불러오기" })).not.toBeInTheDocument();
    expect(getSchedule.mock.calls[1]?.[0]).toMatchObject({ cursor: "schedule-page-2" });
  });

  it("keeps loaded matches visible while retrying a recoverable page error", async () => {
    const firstMatch = serializedApiFixture.schedule.data.items[0];
    const secondMatch = {
      ...firstMatch,
      id: "match-page-retry",
      home_team: { ...firstMatch.home_team, id: "retry-home", name: "Retry Home Club" },
      away_team: { ...firstMatch.away_team, id: "retry-away", name: "Retry Away Club" },
    };
    const getSchedule = vi.fn<OperatorApiClient["getSchedule"]>()
      .mockResolvedValueOnce(schedulePage([firstMatch], "retry-page"))
      .mockRejectedValueOnce(new Error("page temporarily unavailable"))
      .mockResolvedValueOnce(schedulePage([secondMatch], null));
    const client: OperatorApiClient = {
      dataMode: "synthetic-test",
      getSchedule,
      getMatch: () => Promise.resolve(serializedApiFixture.match),
    };
    const user = userEvent.setup();
    renderBriefing(client);

    await user.click(await screen.findByRole("button", { name: "다음 경기 불러오기" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("page temporarily unavailable");
    expect(screen.getAllByRole("button", { name: /Home Club/ }).find((button) => button.getAttribute("aria-pressed") === "true")).toBeDefined();

    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByRole("button", { name: /Retry Home Club/ })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("refreshes the schedule from the first page when a cursor revision is stale", async () => {
    const firstMatch = serializedApiFixture.schedule.data.items[0];
    const getSchedule = vi.fn<OperatorApiClient["getSchedule"]>()
      .mockResolvedValueOnce(schedulePage([firstMatch], "stale-schedule-page"))
      .mockRejectedValueOnce(new OperatorApiError(409, {
        code: "cursor_revision_stale",
        message: "schedule changed",
        retryable: true,
        correlation_id: "cursor-stale",
      }))
      .mockResolvedValueOnce(schedulePage([firstMatch], null));
    const client: OperatorApiClient = {
      dataMode: "synthetic-test",
      getSchedule,
      getMatch: () => Promise.resolve(serializedApiFixture.match),
    };
    const user = userEvent.setup();
    renderBriefing(client);

    await user.click(await screen.findByRole("button", { name: "다음 경기 불러오기" }));

    await waitFor(() => expect(getSchedule).toHaveBeenCalledTimes(3));
    expect(getSchedule.mock.calls[1]?.[0].cursor).toBe("stale-schedule-page");
    expect(getSchedule.mock.calls[2]?.[0].cursor).toBeUndefined();
    expect(await screen.findByRole("heading", { name: /Home Club 승리 확률 60%/ })).toBeInTheDocument();
  });

  it("keeps loaded matches on a page error and aborts that request when filters change", async () => {
    const firstMatch = serializedApiFixture.schedule.data.items[0];
    let pageSignal: AbortSignal | undefined;
    const getSchedule = vi.fn<OperatorApiClient["getSchedule"]>((query, signal) => {
      if (!query.cursor) return Promise.resolve(schedulePage([firstMatch], query.date === "2026-09-20" ? "page-2" : null));
      pageSignal = signal;
      return new Promise<ScheduleResponse>(() => undefined);
    });
    const client: OperatorApiClient = {
      dataMode: "synthetic-test",
      getSchedule,
      getMatch: () => Promise.resolve(serializedApiFixture.match),
    };
    const user = userEvent.setup();
    renderBriefing(client);

    await user.click(await screen.findByRole("button", { name: "다음 경기 불러오기" }));
    expect(screen.getByRole("button", { name: /Home Club/ })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("경기일 · KST"), { target: { value: "2026-09-21" } });

    await waitFor(() => expect(pageSignal?.aborted).toBe(true));
    await waitFor(() => expect(getSchedule.mock.calls.at(-1)?.[0].date).toBe("2026-09-21"));
    expect(screen.queryByText("다음 일정을 불러오지 못했습니다")).not.toBeInTheDocument();
  });

  it("ignores an initial schedule response that resolves after the date changes", async () => {
    const freshMatch = serializedApiFixture.schedule.data.items[0];
    const staleMatch = {
      ...freshMatch,
      id: "stale-match",
      home_team: { ...freshMatch.home_team, id: "stale-home", name: "Stale Home Club" },
      away_team: { ...freshMatch.away_team, id: "stale-away", name: "Stale Away Club" },
    };
    let resolveStale!: (value: ScheduleResponse) => void;
    const getSchedule = vi.fn<OperatorApiClient["getSchedule"]>((query) => {
      if (query.date === "2026-09-20") {
        return new Promise<ScheduleResponse>((resolve) => {
          resolveStale = resolve;
        });
      }
      return Promise.resolve(schedulePage([freshMatch], null));
    });
    const client: OperatorApiClient = {
      dataMode: "synthetic-test",
      getSchedule,
      getMatch: () => Promise.resolve(serializedApiFixture.match),
    };
    renderBriefing(client);

    fireEvent.change(screen.getByLabelText("경기일 · KST"), { target: { value: "2026-09-21" } });
    expect(await screen.findByRole("button", { name: /^여자부.*Home Club.*Away Club/ })).toBeInTheDocument();

    await act(async () => {
      resolveStale(schedulePage([staleMatch], null));
      await Promise.resolve();
    });
    expect(screen.queryByRole("button", { name: /Stale Home Club/ })).not.toBeInTheDocument();
    expect(screen.getByLabelText("경기일 · KST")).toHaveValue("2026-09-21");
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

  it("shows a malformed successful schedule response as an error panel", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        ...serializedApiFixture.schedule,
        data: {
          ...serializedApiFixture.schedule.data,
          items: [
            {
              ...serializedApiFixture.schedule.data.items[0],
              provider_outcomes: null,
            },
          ],
        },
      }),
    }));

    renderBriefing(createOperatorApiClient({ token: "fixture-token" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "API response does not match the operator envelope",
    );
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
    expect(screen.getAllByText("기준 이후 수신").length).toBeGreaterThan(0);
    expect(screen.getByText("market-google-late")).toBeInTheDocument();
    expect(screen.getByText("received_after_input_cutoff")).toBeInTheDocument();
    expect(screen.getByText("이 시각까지 고정된 입력만 사용")).toBeInTheDocument();
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
      json: () => Promise.resolve(serializedApiFixture.schedule),
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = createOperatorApiClient({ token: "fixture-token" });

    await client.getSchedule({ date: "2026-09-20", timezone: "Asia/Seoul", division: "women", cursor: "opaque-page-2" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/schedule?date=2026-09-20&timezone=Asia%2FSeoul&limit=100&division=women&cursor=opaque-page-2",
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
