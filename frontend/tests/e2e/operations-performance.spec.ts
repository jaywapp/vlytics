import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installOperatorSession,
  metadata,
  operationsResponse,
  performanceResponse,
  performanceRows,
  retryResponse,
  retryableError,
} from "./fixtures";

test.beforeEach(async ({ page }) => installOperatorSession(page));

test("keeps operations usable during partial failure and enforces deadline and idempotent retry", async ({ page }) => {
  const retryKeys: string[] = [];
  let retryAttempts = 0;
  const operationsQueries: URL[] = [];

  await page.route("**/api/v1/operations/coverage", (route) => fulfillJson(route, retryableError, 503));
  await page.route("**/api/v1/operations?**", async (route) => {
    operationsQueries.push(new URL(route.request().url()));
    return fulfillJson(route, operationsResponse);
  });
  await page.route("**/api/v1/jobs/*/retry", async (route) => {
    retryAttempts += 1;
    retryKeys.push(route.request().headers()["idempotency-key"] ?? "");
    if (retryAttempts === 1) {
      await new Promise((resolve) => setTimeout(resolve, 150));
      return fulfillJson(route, retryableError, 503);
    }
    return fulfillJson(route, retryResponse);
  });

  await page.goto("/operations");
  await expect(page.getByText("provider.openai")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("Coverage만 불러오지 못했습니다");
  await expect(page.getByText("마감 지남")).toBeVisible();

  const activeRow = page.getByRole("row", { name: /provider\.openai/ });
  const expiredRow = page.getByRole("row", { name: /mirror\.sync/ });
  await expect(activeRow.getByRole("button", { name: "재시도" })).toHaveCount(1);
  await expect(expiredRow.getByRole("button", { name: "재시도" })).toHaveCount(0);

  await activeRow.getByRole("button", { name: "재시도" }).click();
  await expect(activeRow.getByRole("button", { name: "요청 중" })).toBeDisabled();
  await expect(activeRow.getByRole("alert")).toContainText("fixture service unavailable");
  await activeRow.getByRole("button", { name: "재시도" }).click();
  await expect.poll(() => retryKeys.length).toBe(2);
  expect(retryKeys[0]).toBeTruthy();
  expect(retryKeys[1]).toBe(retryKeys[0]);

  await page.getByLabel("상태").selectOption("failed");
  await page.getByLabel("작업 유형").fill("provider.openai");
  await page.getByRole("button", { name: "필터 적용" }).click();
  await expect(page).toHaveURL(/state=failed/);
  await expect.poll(() => operationsQueries.at(-1)?.searchParams.get("job_type")).toBe("provider.openai");
});

test("renders server-owned n20, n1, n0, confidence interval, and missing-market semantics", async ({ page }) => {
  await page.route("**/api/v1/performance**", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 150));
    const model = new URL(route.request().url()).searchParams.get("model");
    const rows = model ? performanceRows.filter((row) => row.model_version === model) : performanceRows;
    return fulfillJson(route, performanceResponse(rows));
  });

  await page.goto("/performance");
  await expect(page.getByText("서버 집계를 불러오는 중입니다")).toBeVisible();
  await expect(page.getByRole("heading", { name: "openai · model-n20" })).toBeVisible();
  await expect(page.locator(".performance-counts").getByText("평가 n", { exact: true }).locator("..")).toContainText("20");
  await expect(page.getByRole("table", { name: /95% 신뢰구간/ })).toContainText("-0.0513");
  await expect(page.getByText("시장 데이터 미수신").first()).toBeVisible();

  await page.getByLabel("모델 버전").selectOption("model-n1");
  await expect(page.locator(".performance-counts").getByText("평가 n", { exact: true }).locator("..")).toContainText("1");
  await expect(page.getByText(/짝비교 n이 2보다 작은/)).toBeVisible();
  await expect(page.getByRole("table", { name: /95% 신뢰구간/ })).toContainText("산출 불가");
  await expect(page.getByText("시장 데이터 미수신").first()).toBeVisible();

  await page.getByLabel("모델 버전").selectOption("model-n0");
  await expect(page.locator(".performance-counts").getByText("평가 n", { exact: true }).locator("..")).toContainText("0");
  await expect(page.getByRole("table", { name: /서버 집계 성능 지표/ })).toContainText("산출 불가");
  await expect(page.getByRole("heading", { name: "보정 집계가 없습니다" })).toBeVisible();
});

test("offers a retry after a performance API failure", async ({ page }) => {
  let attempts = 0;
  await page.route("**/api/v1/performance**", async (route) => {
    attempts += 1;
    if (attempts === 1) return fulfillJson(route, retryableError, 503);
    return fulfillJson(route, { metadata, data: { cohort_policy_version: "performance-cohort-v1", items: [] } });
  });

  await page.goto("/performance");
  await expect(page.getByRole("alert")).toContainText("fixture service unavailable");
  await page.getByRole("button", { name: "다시 시도" }).click();
  await expect(page.getByRole("heading", { name: "조건에 맞는 평가가 없습니다" })).toBeVisible();
});
