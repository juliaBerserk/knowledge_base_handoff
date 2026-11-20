import { expect, test } from "@playwright/test";

test.use({ timezoneId: "Europe/Moscow" });

test("HR планирует эстафету и видит чек-лист", async ({ page }) => {
  test.skip(process.env.E2E !== "1", "Нужен запущенный интерфейс");
  const plan = {
    id: "handoff", kind: "departure", status: "scheduled", employee_id: "employee",
    employee_name: "Уходящий сотрудник", position_title: "Специалист", successor_name: "Преемник",
    effective_at: "2027-01-14T15:00:00Z", successor_id: "successor",
    completeness: { people: 0, risks: 1 }, can_cancel: true, can_start: false,
  };
  let scheduled = false;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/session") {
      await route.fulfill({ json: {
        employee_id: "hr", role: "hr", csrf_token: "fixture", enrollment_only: false,
      } });
    } else if (path === "/api/health") {
      await route.fulfill({ json: { ok: true, database: true, vector_extension: true } });
    } else if (path === "/api/handoffs/options") {
      await route.fulfill({ json: {
        positions: [],
        employees: [
          { id: "employee", name: "Уходящий сотрудник", position_id: "position",
            position_title: "Специалист", can_succeed: true },
          { id: "successor", name: "Преемник", position_id: null, can_succeed: true },
        ],
      } });
    } else if (path === "/api/handoffs" && route.request().method() === "POST") {
      expect(route.request().postDataJSON()).toEqual({
        employee_id: "employee", position_id: "position", successor_id: "successor",
        target_position_id: null,
        effective_at: "2027-01-14T15:00:00.000Z",
      });
      scheduled = true;
      await route.fulfill({ status: 201, json: { id: "handoff" } });
    } else if (path === "/api/handoffs") {
      await route.fulfill({ json: { items: scheduled ? [plan] : [], next_before: null } });
    } else if (path === "/api/handoffs/handoff") {
      await route.fulfill({ json: plan });
    } else {
      await route.fulfill({ status: 404 });
    }
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Создать эстафету или перевод" }).click();
  await page.getByRole("combobox", { name: "Сотрудник", exact: true }).selectOption("employee");
  await page.getByRole("combobox", { name: "Преемник", exact: true }).selectOption("successor");
  await page.getByLabel("Последний рабочий день").fill("2027-01-14");
  await page.getByLabel(/Время блокировки/).fill("18:00");
  await page.getByRole("button", { name: "Запланировать" }).click();
  await expect(page.getByText("План сохранён")).toBeVisible();
  await expect(page.getByText("Контакты · Записей пока нет")).toBeVisible();
  await expect(page.getByText("Риски · 1")).toBeVisible();
  await expect(page.getByRole("button", { name: "С чего начать" })).toHaveCount(0);
});
