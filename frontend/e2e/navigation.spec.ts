import { expect, test } from "@playwright/test";

const fragment = "4256a950-aa91-4194-aa2f-04986c1ceb9b";
const material = "0ea35394-22c7-4269-949a-571580627363";

test("страница источника показывает очищенный текст и ссылку на материал", async ({ page }) => {
  test.skip(process.env.E2E !== "1", "Нужен запущенный интерфейс");
  await page.route(`**/api/build/sources/${fragment}`, (route) => route.fulfill({ json: {
    material_id: material, text: "<script>Текст источника</script>",
  } }));
  await page.route(`**/api/publication/materials/${material}`, (route) => route.fulfill({ json: {
    id: material, text: "Очищенный материал", status: "published",
  } }));
  await page.goto(`/sources/${fragment}`);
  await expect(page.getByRole("heading", { name: "Фрагмент источника" })).toBeVisible();
  await expect(page.locator("pre")).toHaveText("<script>Текст источника</script>");
  await expect(page.locator("pre script")).toHaveCount(0);
  await page.getByRole("link", { name: "Открыть материал целиком" }).click();
  await expect(page.getByRole("heading", { name: "Материал", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Скачать очищенный текст" })).toHaveAttribute(
    "href", `/api/publication/materials/${material}/export`,
  );
});

test("при отзыве доступа страница источника показывает русское сообщение", async ({ page }) => {
  test.skip(process.env.E2E !== "1", "Нужен запущенный интерфейс");
  await page.route(`**/api/build/sources/${fragment}`, (route) => route.fulfill({ status: 404 }));
  await page.goto(`/sources/${fragment}`);
  await expect(page.getByRole("alert")).toHaveText("Источник недоступен для вашей учётной записи.");
  await expect(page.getByRole("link", { name: "Открыть материал целиком" })).toHaveCount(0);
});

test("аудитор показывает события и переходит к предыдущей странице", async ({ page }) => {
  test.skip(process.env.E2E !== "1", "Нужен запущенный интерфейс");
  await page.route("**/api/auth/session", (route) => route.fulfill({ json: {
    employee_id: "auditor", role: "security_auditor", csrf_token: "fixture", enrollment_only: false,
  } }));
  await page.route(/\/api\/audit\/events\?/, (route) => {
    const params = new URL(route.request().url()).searchParams;
    expect(params.get("action")).toBe("knowledge_exported");
    const older = params.get("before_id") === "41";
    return route.fulfill({ json: {
      events: [{
        id: older ? 40 : 41, occurred_at: "2026-11-20T12:00:00Z", action: "knowledge_exported",
        outcome: "success", actor_id: older ? "предыдущий" : "текущий",
        target_kind: "material", target_id: material,
      }],
      next_before_id: older ? null : 41,
    } });
  });
  await page.goto("/");
  await expect(page.getByRole("navigation").getByRole("link", { name: "Журнал аудита" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Материалы и записи" })).toHaveCount(0);
  await page.getByRole("combobox", { name: "Действие" }).selectOption("knowledge_exported");
  await page.getByRole("button", { name: "Показать события" }).click();
  await expect(page.getByRole("cell", { name: "Экспорт", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Более ранние события" }).click();
  await expect(page.getByText("предыдущий", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Более ранние события" })).toHaveCount(0);
});
