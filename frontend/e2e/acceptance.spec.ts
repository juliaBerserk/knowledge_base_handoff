import { readFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";

import { expect, test } from "@playwright/test";

test.use({ trace: "off" });

test("рабочий экран сотрудника готов за две секунды", async ({ baseURL, context, page }) => {
  const path = process.env.E2E_ACCEPTANCE_MANIFEST;
  test.skip(!path, "Нужны отдельный стенд и файл с тестовыми сессиями");
  const manifest = JSON.parse(await readFile(path!, "utf8")) as {
    origin: string; sessions: { cookie: string }[];
  };
  const origin = new URL(baseURL!).origin;
  expect(origin).toBe(manifest.origin);
  const cookieURL = new URL("/", origin);
  cookieURL.protocol = "https:";
  await context.addCookies([{
    name: "__Host-estafeta_session",
    value: manifest.sessions[0].cookie,
    url: cookieURL.href,
    httpOnly: true,
    secure: true,
    sameSite: "Lax",
  }]);

  const started = performance.now();
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Поиск и вопросы по базе" })).toBeVisible();
  expect(performance.now() - started).toBeLessThanOrEqual(2000);
});
