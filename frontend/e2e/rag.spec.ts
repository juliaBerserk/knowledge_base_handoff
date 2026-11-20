import { expect, test } from "@playwright/test";

test("интерфейс показывает выдачу, цитату SSE и сохраняет оценку", async ({ page }) => {
  test.skip(process.env.E2E !== "1", "Нужен запущенный интерфейс");
  let feedback = false;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/session") {
      await route.fulfill({ json: {
        employee_id: "employee", role: "employee", totp_enabled: false,
        csrf_token: "fixture", enrollment_only: false,
      } });
    } else if (path === "/api/build") {
      await route.fulfill({ json: { job: null } });
    } else if (path === "/api/health") {
      await route.fulfill({ json: { ok: true, database: true, vector_extension: true } });
    } else if (path === "/api/search") {
      expect(route.request().postDataJSON().scope).toBe("department");
      await route.fulfill({ json: {
        results: [{ id: "chunk", material_id: "material", text: "Регламент смены" }],
        semantic_available: true,
      } });
    } else if (path === "/api/chat/ask") {
      const packets = [
        ["started", { chat_id: "chat", question: "Как начать смену?" }],
        ["citation", { chunk_id: "chunk", material_id: "material", text: "Проверьте журнал." }],
        ["done", { turn_id: "turn", message: "" }],
      ];
      await route.fulfill({
        contentType: "text/event-stream",
        body: packets.map(([event, data]) =>
          `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(""),
      });
    } else if (path === "/api/chat/turns/turn/feedback") {
      const data = route.request().postDataJSON();
      expect(data).toEqual({ rating: 1, comment: "Понятно" });
      feedback = true;
      await route.fulfill({ json: data });
    } else {
      await route.fulfill({ status: 404 });
    }
  });
  await page.goto("/");
  const panel = page.getByRole("heading", { name: "Поиск и вопросы по базе" }).locator("..");
  await panel.getByLabel("Область").selectOption("department");
  await panel.getByLabel("Запрос").fill("Как начать смену?");
  await panel.getByRole("button", { name: "Найти", exact: true }).click();
  await expect(panel.getByText("Регламент смены")).toBeVisible();
  await panel.getByRole("button", { name: "Спросить ИИ" }).click();
  await expect(panel.getByText("Проверьте журнал.")).toBeVisible();
  await expect(panel.getByRole("link", { name: "Открыть источник" })).toHaveAttribute(
    "href", "/sources/chunk",
  );
  await panel.getByText("Оценить ответ").click();
  await panel.getByLabel("Комментарий").fill("Понятно");
  await panel.getByRole("button", { name: "Полезный ответ", exact: true }).click();
  await expect(panel.getByText("Оценка сохранена")).toBeVisible();
  expect(feedback).toBe(true);
});
