import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

type SeedManifest = {
  schedule_date: string;
  match_id: string;
  job_id: string;
  competition: string;
  home_team: string;
  away_team: string;
};

const token = process.env.VLYTICS_LIVE_E2E_OPERATOR_TOKEN;
const manifestPath = process.env.VLYTICS_LIVE_E2E_SEED_MANIFEST;
if (!token) throw new Error("VLYTICS_LIVE_E2E_OPERATOR_TOKEN is required");
if (!manifestPath) throw new Error("VLYTICS_LIVE_E2E_SEED_MANIFEST is required");
const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as SeedManifest;
const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

test("uses the real Nginx, FastAPI, and PostgreSQL operator flow", async ({ page }) => {
  await page.addInitScript((operatorToken) => {
    window.sessionStorage.setItem("vlytics.operator-token", operatorToken);
  }, token);

  const scheduleResponse = page.waitForResponse(
    (response) => response.url().includes("/api/v1/schedule?") && response.status() === 200,
  );
  await page.goto("/");
  await scheduleResponse;
  await page.getByLabel("경기일 · KST").fill(manifest.schedule_date);
  const seededMatch = page.getByRole("button", {
    name: new RegExp(escapeRegExp(manifest.home_team)),
  });
  await expect(seededMatch).toBeVisible();
  await seededMatch.click();
  await expect(
    page.getByRole("heading", {
      name: new RegExp(`${escapeRegExp(manifest.home_team)} 승리 확률 58%`),
    }),
  ).toBeVisible();
  const comparison = page.getByRole("table", { name: /통계 · GPT · Claude · Gemini/ });
  await expect(comparison.getByText("GPT 독립 실험군")).toBeVisible();
  await expect(comparison.getByText("통계 세트 모델")).toBeVisible();
  const gptRow = comparison.getByRole("row", { name: /GPT 독립 실험군/ });
  await expect(gptRow).toContainText("64%");
  await gptRow.getByRole("button", { name: "분석 보기" }).click();
  await expect(
    page.getByRole("heading", {
      name: new RegExp(`${escapeRegExp(manifest.home_team)} 승리 확률 64%`),
    }),
  ).toBeVisible();

  await page.getByRole("link", { name: "예측 기록" }).click();
  await page.getByLabel("대회").fill(manifest.competition);
  await page.getByLabel("Provider").fill("openai");
  await page.getByRole("button", { name: "필터 적용" }).click();
  await expect(page.getByRole("heading", { name: "openai · winner" })).toBeVisible();
  await expect(page.getByText("게시됨")).toBeVisible();

  await page.getByRole("link", { name: "운영" }).click();
  await page.getByLabel("작업 유형").fill("live-e2e.retry");
  await page.getByRole("button", { name: "필터 적용" }).click();
  const jobRow = page.getByRole("row", { name: new RegExp(manifest.job_id) });
  await expect(jobRow).toContainText("live_e2e_failure");
  const retryResponsePromise = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/api/v1/jobs/${manifest.job_id}/retry`) &&
      response.request().method() === "POST",
  );
  await jobRow.getByRole("button", { name: "재시도" }).click();
  const retryResponse = await retryResponsePromise;
  expect(retryResponse.status()).toBe(200);
  expect(await retryResponse.json()).toMatchObject({
    data: { job_id: manifest.job_id, state: "retry_wait" },
  });

  await page.getByRole("link", { name: "성능" }).click();
  await expect(page.getByLabel("대회")).toContainText(manifest.competition);
  const competitionResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/v1/performance" &&
      url.searchParams.get("competition") === manifest.competition &&
      !url.searchParams.has("provider") &&
      response.status() === 200
    );
  });
  await page.getByLabel("대회").selectOption(manifest.competition);
  await competitionResponse;
  const providerResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/v1/performance" &&
      url.searchParams.get("competition") === manifest.competition &&
      url.searchParams.get("provider") === "openai" &&
      response.status() === 200
    );
  });
  await page.getByLabel("Provider").selectOption("openai");
  await providerResponse;
  await expect(page.getByRole("heading", { name: "openai · live-openai-model" })).toBeVisible();
  const performanceRow = page
    .getByRole("table", { name: /서버 집계 성능 지표/ })
    .getByRole("row", { name: /openai · live-openai-model/ });
  await expect(performanceRow).toContainText("100.0%");
  await expect(performanceRow).toContainText("0.1296");
  await expect(performanceRow).toContainText("0.4460");
  await expect(performanceRow.getByRole("cell", { name: "1", exact: true })).toBeVisible();
  await expect(page.getByText("운영 API")).toBeVisible();
});
