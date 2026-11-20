import { expect, test } from "@playwright/test";

test("администратор проверяет и подтверждает CSV", async ({ page }) => {
  test.skip(process.env.E2E !== "1", "Нужен запущенный интерфейс");
  const csv = "ФИО;корпоративная почта;отдел;должность;роль\nТестовый сотрудник;;ИТ;Специалист;Сотрудник\n";
  let imported = false;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/session") {
      await route.fulfill({ json: {
        employee_id: "admin", role: "admin", totp_enabled: true,
        csrf_token: "fixture", enrollment_only: false,
      } });
    } else if (path === "/api/health") {
      await route.fulfill({ json: { ok: true, database: true, vector_extension: true } });
    } else if (path === "/api/admin/accounts/import/preview") {
      expect(route.request().postDataJSON()).toEqual({ csv_text: csv });
      expect(imported).toBe(false);
      await route.fulfill({ json: { count: 1 } });
    } else if (path === "/api/admin/accounts/import") {
      expect(route.request().headers()["x-csrf-token"]).toBe("fixture");
      expect(route.request().postDataJSON()).toEqual({ csv_text: csv, send_email: true });
      imported = true;
      await route.fulfill({ status: 201, json: {
        created: 1, accounts: [{ row: 2, id: "employee", url: "http://localhost/#invite=fixture", sent: false }],
      } });
    } else {
      await route.fulfill({ status: 404 });
    }
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Импорт CSV" }).click();
  await page.getByLabel("Файл CSV").setInputFiles({
    name: "accounts.csv", mimeType: "text/csv", buffer: Buffer.from(csv, "utf-8"),
  });
  await expect(page.getByRole("button", { name: "Импортировать" })).toHaveCount(0);
  await page.getByRole("button", { name: "Проверить CSV" }).click();
  await expect(page.getByText("Готово к импорту 1 учётных записей.")).toBeVisible();
  await page.getByRole("button", { name: "Импортировать" }).click();
  await expect(page.getByText("Импортировано учётных записей: 1")).toBeVisible();
  await expect(page.getByLabel("Одноразовая ссылка")).toHaveValue(/#invite=fixture$/);
  expect(imported).toBe(true);
});
