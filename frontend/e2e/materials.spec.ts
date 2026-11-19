import { expect, test } from "@playwright/test";

test("сотрудник проверяет заметку и сохраняет очищенный черновик", async ({ page }) => {
  const login = process.env.E2E_MATERIAL_LOGIN;
  const password = process.env.E2E_MATERIAL_PASSWORD;
  test.skip(!login || !password, "Нужна учётная запись сотрудника из отдельной тестовой БД");
  await page.goto("/");
  await page.getByLabel("Корпоративная почта или идентификатор").fill(login!);
  await page.getByLabel("Пароль", { exact: true }).fill(password!);
  await page.getByRole("button", { name: "Войти" }).click();
  const source = "Пароль: e2e-example-secret\nПоставщик vendor@outside.example\nСлужебная отметка";
  await page.getByLabel("Текст заметки").fill(source);
  await page.getByRole("button", { name: "Проверить и очистить" }).click();
  const cleaned = page.getByLabel("Очищенный текст");
  await expect(cleaned).toHaveValue(/\[ПАРОЛЬ\]/);
  await expect(cleaned).not.toHaveValue(/e2e-example-secret/);
  await expect(cleaned).not.toHaveValue(/vendor@outside\.example/);
  await page.getByText(/Скрытые внешние контакты/).click();
  await page.getByRole("button", { name: "Отметить рабочим" }).click();
  await expect(cleaned).toHaveValue(/vendor@outside\.example/);

  await cleaned.evaluate((element: HTMLTextAreaElement) => {
    const text = "Служебная отметка";
    const start = element.value.indexOf(text);
    element.setSelectionRange(start, start + text.length);
  });
  await page.getByRole("button", { name: "Скрыть выделенное" }).click();
  await expect(cleaned).toHaveValue(/\[СКРЫТО\]/);
  await expect(cleaned).not.toHaveValue(/Служебная отметка/);
  await page.getByRole("button", { name: "Сохранить очищенный текст" }).click();
  await expect(page.getByText("Очищенный материал сохранён как личный черновик")).toBeVisible();
});
