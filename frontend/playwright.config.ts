import { defineConfig, devices } from "@playwright/test";

const managed = process.env.E2E_VITE === "1";
const managedURL = "http://127.0.0.1:4173";
const baseURL = managed ? managedURL : (process.env.E2E_BASE_URL ?? "http://127.0.0.1:8080");
const chromiumExecutable = process.env.E2E_BROWSER_EXECUTABLE;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  webServer: managed ? {
    command: "npm run dev -- --host 127.0.0.1 --port 4173 --strictPort",
    url: managedURL,
    reuseExistingServer: !process.env.CI,
  } : undefined,
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        channel: chromiumExecutable ? undefined : process.env.E2E_BROWSER_CHANNEL,
        launchOptions: { executablePath: chromiumExecutable },
      },
    },
    ...(process.env.E2E_FIREFOX === "1" ? [
      { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    ] : []),
  ],
});
