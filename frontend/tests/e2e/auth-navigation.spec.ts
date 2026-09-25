import { expect, test } from "@playwright/test";

import {
  expectOperatorAuth,
  fulfillJson,
  historyResponse,
  matchResponse,
  operatorToken,
  scheduleResponse,
} from "./fixtures";

test("authenticates with the keyboard, keeps the token out of URLs, and supports keyboard navigation", async ({ page }) => {
  const requestedUrls: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    expectOperatorAuth(route);
    requestedUrls.push(route.request().url());
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/schedule") return fulfillJson(route, scheduleResponse);
    if (url.pathname.startsWith("/api/v1/matches/")) return fulfillJson(route, matchResponse);
    if (url.pathname === "/api/v1/predictions") return fulfillJson(route, historyResponse);
    return route.abort("failed");
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "운영자 인증이 필요합니다" })).toBeVisible();

  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "vlytics" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("운영자 토큰")).toBeFocused();
  await page.keyboard.type(operatorToken);
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");

  await expect(page.getByRole("heading", { name: /한강 블루웨이브 승리 확률/ })).toBeVisible();
  await expect(page).toHaveURL((url) => !url.href.includes(operatorToken));
  expect(requestedUrls.every((url) => !url.includes(operatorToken))).toBe(true);

  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "본문으로 이동" })).toBeFocused();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "예측 기록" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/history$/);
  await expect(page.getByRole("heading", { name: "예측 기록" })).toBeVisible();
});

for (const viewport of [
  { name: "mobile", width: 390, height: 844 },
  { name: "desktop", width: 1440, height: 1000 },
]) {
  test(`renders the production app without page overflow at ${viewport.name} width`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.addInitScript((token) => sessionStorage.setItem("vlytics.operator-token", token), operatorToken);
    await page.route("**/api/v1/**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/v1/schedule") return fulfillJson(route, scheduleResponse);
      if (pathname.startsWith("/api/v1/matches/")) return fulfillJson(route, matchResponse);
      return route.abort("failed");
    });

    await page.goto("/");
    await expect(page.getByRole("heading", { name: /한강 블루웨이브 승리 확률/ })).toBeVisible();
    const hasPageOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    expect(hasPageOverflow).toBe(false);
    await expect(page.getByRole("navigation", { name: "주요 메뉴" })).toBeVisible();
  });
}
