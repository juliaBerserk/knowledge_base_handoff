import { createHmac } from "node:crypto";

import { expect, test } from "@playwright/test";

function totp(key: string, step: number): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const bits = [...key].map((char) => alphabet.indexOf(char).toString(2).padStart(5, "0")).join("");
  const secret = Buffer.from((bits.match(/.{8}/g) ?? []).map((byte) => parseInt(byte, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const digest = createHmac("sha1", secret).update(counter).digest();
  const value = digest.readUInt32BE(digest[digest.length - 1] & 15) & 0x7fffffff;
  return (value % 1_000_000).toString().padStart(6, "0");
}

test("первый администратор задаёт пароль и входит с TOTP", async ({ page }) => {
  const invitation = process.env.E2E_BOOTSTRAP_URL;
  test.skip(!invitation, "Нужна свежая ссылка bootstrap из отдельной тестовой БД");
  const password = "Пароль с апельсином для тестового входа!";

  await page.goto(invitation!);
  await page.getByLabel("Новый пароль").fill(password);
  await page.getByLabel("Повторите пароль").fill(password);
  await page.getByRole("button", { name: "Сохранить пароль" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Пароль задан" })).toBeVisible();
  expect(new URL(page.url()).hash).toBe("");
  const login = await page.getByRole("textbox", { name: "Корпоративная почта или идентификатор" }).inputValue();
  await page.getByLabel("Пароль", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page.getByText("Для входа настройте двухфакторную защиту.")).toBeVisible();
  const issueInvitation = page.getByRole("button", { name: "Выпустить ссылку" });
  await expect(issueInvitation).toHaveCount(0);

  await page.getByRole("button", { name: "Настроить двухфакторную защиту" }).click();
  const key = await page.getByLabel("Ключ настройки").inputValue();
  const step = Math.floor(Date.now() / 30_000);
  await page.getByLabel("Код из приложения").fill(totp(key, step));
  await page.getByRole("button", { name: "Подтвердить" }).click();
  await expect(issueInvitation).toBeVisible();

  await page.getByRole("button", { name: "Выйти" }).click();
  await expect(page.getByRole("heading", { name: "Вход" })).toBeVisible();
  await page.getByRole("textbox", { name: "Корпоративная почта или идентификатор" }).fill(login);
  await page.getByLabel("Пароль", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
  await page.getByLabel("Код из приложения").fill(totp(key, step + 1));
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(issueInvitation).toBeVisible();
});
