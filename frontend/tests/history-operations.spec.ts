import "@testing-library/jest-dom/vitest";

import { createElement } from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HistoryPage } from "../src/features/history/HistoryPage";
import type { HistoryApiClient, HistoryQuery, PredictionHistoryItem, PredictionHistoryResponse } from "../src/features/history/types";
import { OperationsPage } from "../src/features/operations/OperationsPage";
import type { CoverageResponse, OperationsApiClient, OperationsResponse } from "../src/features/operations/types";

const metadata = {
  schema_version: "vlytics.operator.v1" as const,
  source_snapshot_ids: ["source-1"],
  schedule_revision_ids: ["schedule-1"],
  prediction_revision_ids: ["prediction-1"],
  evaluation_revision_ids: ["evaluation-1"],
  model_versions: { openai: ["model-v1"] },
};

const prediction: PredictionHistoryItem = {
  id: "prediction-1",
  match_id: "match-1",
  competition: "synthetic-regular",
  division: "women",
  provider: "openai",
  prediction_type: "winner",
  requested_model: "requested-v1",
  resolved_model_id: "resolved-v1",
  model_version: "model-v1",
  prompt_version: "prompt-v3",
  feature_version: "feature-v2",
  schedule_revision_id: "schedule-1",
  source_snapshot_id: "source-1",
  generated_at: "2026-09-20T08:00:00Z",
  status: "succeeded",
  output: {
    home_win_probability: 0.61,
    capabilities: ["winner"],
    api_key: "must-never-render",
    raw_payload: "private-provider-response",
  },
  evaluation_revision_id: "evaluation-1",
};

function historyResponse(items = [prediction], nextCursor: string | null = "opaque-page-2"): PredictionHistoryResponse {
  return { metadata, data: { items, next_cursor: nextCursor } };
}

function operationsResponse(): OperationsResponse {
  return {
    metadata,
    data: {
      items: [
        {
          id: "11111111-1111-4111-8111-111111111111",
          job_type: "provider.openai",
          state: "failed",
          due_at: "2026-09-20T08:00:00Z",
          deadline_at: "2099-09-20T09:00:00Z",
          attempt_no: 2,
          error_code: "provider_timeout",
          retryable: true,
        },
        {
          id: "22222222-2222-4222-8222-222222222222",
          job_type: "mirror.sync",
          state: "failed",
          due_at: "2026-09-20T08:00:00Z",
          deadline_at: "2020-09-20T09:00:00Z",
          attempt_no: 3,
          error_code: "deadline_expired",
          retryable: true,
        },
      ],
      budgets: [
        {
          provider: "openai",
          currency: "USD",
          period: "day",
          period_start: "2026-09-20",
          period_end: "2026-09-21",
          as_of: "2026-09-20T08:01:00Z",
          reserved_amount: "1.50000000",
          actual_amount: "0.70000000",
          effective_amount: "1.20000000",
          reservation_count: 2,
          settled_count: 1,
          outstanding_count: 1,
          conservative_charge_count: 0,
        },
        {
          provider: "openai",
          currency: "USD",
          period: "month",
          period_start: "2026-09-01",
          period_end: "2026-10-01",
          as_of: "2026-09-20T08:01:00Z",
          reserved_amount: "3.00000000",
          actual_amount: "2.20000000",
          effective_amount: "2.70000000",
          reservation_count: 4,
          settled_count: 3,
          outstanding_count: 1,
          conservative_charge_count: 1,
        },
      ],
      next_cursor: "opaque-jobs-2",
    },
  };
}

const coverageResponse: CoverageResponse = {
  metadata,
  data: {
    items: [
      {
        data_kind: "lineup",
        availability: "missing",
        count: 2,
        latest_observed_at: "2026-09-20T07:55:00Z",
        evidence_codes: ["source_not_published"],
      },
    ],
  },
};

afterEach(() => {
  cleanup();
  window.history.replaceState({}, "", "/");
  vi.restoreAllMocks();
});

describe("prediction history", () => {
  it("keeps compound filters, opaque cursor, detail, and browser state in the URL", async () => {
    window.history.replaceState({}, "", "/history?division=women&provider=openai&start=2026-09-19&end=2026-09-20");
    const queries: HistoryQuery[] = [];
    const client: HistoryApiClient = {
      getPredictions: vi.fn((query: HistoryQuery) => {
        queries.push(query);
        return Promise.resolve(historyResponse());
      }),
    };
    const user = userEvent.setup();
    render(createElement(HistoryPage, { client, onNavigate: vi.fn() }));

    expect(await screen.findByText("openai · winner")).toBeInTheDocument();
    expect(queries[0]).toMatchObject({
      division: "women",
      provider: "openai",
      startAt: "2026-09-19T00:00:00+09:00",
      endAt: "2026-09-21T00:00:00+09:00",
    });
    await user.type(screen.getByLabelText("팀"), "HOME");
    await user.type(screen.getByLabelText("예측 유형"), "winner");
    await user.type(screen.getByLabelText("Prompt version"), "prompt-v3");
    await user.click(screen.getByRole("button", { name: "필터 적용" }));
    await waitFor(() => expect(window.location.search).toContain("team=HOME"));
    expect(window.location.search).toContain("type=winner");
    expect(window.location.search).toContain("prompt=prompt-v3");

    await user.click(await screen.findByRole("button", { name: "다음 기록" }));
    await waitFor(() => expect(queries.at(-1)?.cursor).toBe("opaque-page-2"));
    expect(window.location.search).toContain("cursor=opaque-page-2");

    await user.click(await screen.findByRole("button", { name: "Snapshot 보기" }));
    expect(await screen.findByRole("heading", { name: "불변 예측 Snapshot" })).toBeInTheDocument();
    expect(window.location.search).toContain("prediction=prediction-1");
    expect(screen.getByText("prediction-1")).toBeInTheDocument();
    expect(screen.queryByText("must-never-render")).not.toBeInTheDocument();
    expect(screen.queryByText("private-provider-response")).not.toBeInTheDocument();

    window.history.back();
    await waitFor(() => expect(window.location.search).not.toContain("prediction="));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "불변 예측 Snapshot" })).not.toBeInTheDocument());
    expect(await screen.findByText("openai · winner")).toBeInTheDocument();
    expect(screen.getByLabelText("팀")).toHaveValue("HOME");
  });

  it("shows n0 without inventing records", async () => {
    window.history.replaceState({}, "", "/history");
    const client: HistoryApiClient = { getPredictions: vi.fn(() => Promise.resolve(historyResponse([], null))) };
    render(createElement(HistoryPage, { client, onNavigate: vi.fn() }));
    expect(await screen.findByRole("heading", { name: "조건에 맞는 예측 기록이 없습니다" })).toBeInTheDocument();
    expect(screen.queryByText("openai · winner")).not.toBeInTheDocument();
  });
});

describe("operations", () => {
  it("keeps operations usable when coverage partially fails and blocks expired retry", async () => {
    window.history.replaceState({}, "", "/operations");
    const client: OperationsApiClient = {
      getOperations: vi.fn(() => Promise.resolve(operationsResponse())),
      getCoverage: vi.fn(() => Promise.reject(new Error("coverage unavailable"))),
      retryJob: vi.fn(),
    };
    render(createElement(OperationsPage, { client, onNavigate: vi.fn() }));
    expect(await screen.findByText("provider.openai")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("coverage unavailable");
    const table = screen.getByRole("table", { name: /운영 작업 상태/ });
    expect(within(table).getByText("provider_timeout")).toBeInTheDocument();
    expect(within(table).getByText("마감 지남")).toBeInTheDocument();
    expect(within(table).getAllByRole("button", { name: "재시도" })).toHaveLength(1);
    expect(screen.getByText(/비용 집계: durable ledger/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Provider 비용" })).toBeInTheDocument();
    expect(screen.getByText(/실제.*0.70/)).toBeInTheDocument();
    expect(screen.getByText(/예약.*1.50.*유효.*1.20/)).toBeInTheDocument();
    expect(screen.getByText(/미정산 1.*보수 청구 0/)).toBeInTheDocument();
  });

  it("reuses one idempotency key after failure and guards duplicate in-flight retry", async () => {
    window.history.replaceState({}, "", "/operations");
    let rejectFirst: ((error: Error) => void) | undefined;
    const first = new Promise<never>((_, reject) => { rejectFirst = reject; });
    const retryJob = vi.fn()
      .mockImplementationOnce(() => first)
      .mockResolvedValueOnce({ metadata, data: { job_id: operationsResponse().data.items[0].id, state: "retry_wait", due_at: "2026-09-20T08:00:00Z", deadline_at: "2099-09-20T09:00:00Z", idempotent_replay: true } });
    const client: OperationsApiClient = {
      getOperations: vi.fn(() => Promise.resolve(operationsResponse())),
      getCoverage: vi.fn(() => Promise.resolve(coverageResponse)),
      retryJob,
    };
    const user = userEvent.setup();
    render(createElement(OperationsPage, { client, onNavigate: vi.fn() }));
    const button = await screen.findByRole("button", { name: "재시도" });
    await user.click(button);
    await user.click(button);
    expect(retryJob).toHaveBeenCalledTimes(1);
    rejectFirst?.(new Error("network interrupted"));
    expect(await screen.findByRole("alert")).toHaveTextContent("network interrupted");

    await user.click(screen.getByRole("button", { name: "재시도" }));
    await waitFor(() => expect(retryJob).toHaveBeenCalledTimes(2));
    expect(retryJob.mock.calls[1][1]).toBe(retryJob.mock.calls[0][1]);
    expect(await screen.findByRole("button", { name: "요청 완료" })).toBeDisabled();
  });

  it("shows independent n0 states for jobs and coverage", async () => {
    const client: OperationsApiClient = {
      getOperations: vi.fn(() => Promise.resolve({ ...operationsResponse(), data: { items: [], budgets: [], next_cursor: null } })),
      getCoverage: vi.fn(() => Promise.resolve({ ...coverageResponse, data: { items: [] } })),
      retryJob: vi.fn(),
    };
    render(createElement(OperationsPage, { client, onNavigate: vi.fn() }));
    expect(await screen.findByRole("heading", { name: "조건에 맞는 작업이 없습니다" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Coverage 기록이 없습니다" })).toBeInTheDocument();
  });
});
