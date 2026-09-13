import { defineConfig, devices } from "@playwright/test"

export default defineConfig({
  testDir: "./tests",
  testMatch: "trading-contract.spec.ts",
  timeout: 30_000,
  workers: 1,
  reporter: "list",
  use: { ...devices["Desktop Chrome"], baseURL: "http://127.0.0.1:5178" },
  webServer: {
    command:
      "bun ../node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5178 --strictPort",
    url: "http://127.0.0.1:5178",
    timeout: 30_000,
    reuseExistingServer: false,
  },
})
