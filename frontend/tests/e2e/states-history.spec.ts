import { expect, test } from "@playwright/test";

import {
  emptyScheduleResponse,
  fulfillJson,
  historyResponse,
  installOperatorSession,
  retryableError,
} from "./fixtures";

test.beforeEach(async ({ page }) => installOperatorSession(page));

test("shows loading, retryable error, and honest empty schedule states", async ({ page }) => {
  let attempts = 0;
  await page.route("**/api/v1/schedule?**", async (route) => {
    attempts += 1;
    if (attempts === 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      return fulfillJson(route, retryableError, 503);
    }
    return fulfillJson(route, emptyScheduleResponse);
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "경기 일정을 불러오는 중입니다" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("fixture service unavailable");
  await page.getByRole("button", { name: "다시 시도" }).click();
  await expect(page.getByRole("heading", { name: "표시할 경기가 없습니다" })).toBeVisible();
  expect(attempts).toBe(2);
});

test("keeps history filters through detail and browser back navigation", async ({ page }) => {
  const predictionRequests: URL[] = [];
  await page.route("**/api/v1/predictions?**", async (route) => {
    predictionRequests.push(new URL(route.request().url()));
    return fulfillJson(route, historyResponse);
  });

  await page.goto("/history?division=women&provider=openai");
  await expect(page.getByRole("heading", { name: "openai · winner" })).toBeVisible();
  await page.getByLabel("팀").fill("HOME");
  await page.getByLabel("예측 유형").fill("winner");
  await page.getByRole("button", { name: "필터 적용" }).click();
  await expect(page).toHaveURL(/team=HOME/);
  await expect.poll(() => predictionRequests.at(-1)?.searchParams.get("team")).toBe("HOME");

  await page.getByRole("button", { name: "Snapshot 보기" }).click();
  await expect(page.getByRole("heading", { name: "불변 예측 Snapshot" })).toBeVisible();
  await expect(page).toHaveURL(/prediction=prediction-e2e-1/);
  await expect(page.getByText("must-never-render")).toHaveCount(0);
  await expect(page.getByText("private-provider-response")).toHaveCount(0);

  await page.goBack();
  await expect(page.getByRole("heading", { name: "openai · winner" })).toBeVisible();
  await expect(page.getByLabel("팀")).toHaveValue("HOME");
  await expect(page.getByLabel("예측 유형")).toHaveValue("winner");
});

test("recovers history from an API error to an empty response", async ({ page }) => {
  let attempts = 0;
  await page.route("**/api/v1/predictions?**", async (route) => {
    attempts += 1;
    if (attempts === 1) return fulfillJson(route, retryableError, 503);
    return fulfillJson(route, { ...historyResponse, data: { items: [], next_cursor: null } });
  });

  await page.goto("/history");
  await expect(page.getByRole("alert")).toContainText("fixture service unavailable");
  await page.getByRole("button", { name: "다시 시도" }).click();
  await expect(page.getByRole("heading", { name: "조건에 맞는 예측 기록이 없습니다" })).toBeVisible();
});
