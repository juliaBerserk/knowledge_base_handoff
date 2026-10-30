import { expect, test } from "@playwright/test";

test("главная страница показывает название системы", async ({ page }) => {
  test.skip(process.env.E2E !== "1", "e2e запускается с E2E=1 при поднятом стеке");
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Эстафета" })).toBeVisible();
});
