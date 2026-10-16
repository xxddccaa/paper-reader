import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const runtime =
  process.env.PAPER_READER_RUNTIME_DIR ||
  path.join(root, "paper-reader-runtime");
process.env.PLAYWRIGHT_BROWSERS_PATH ||= path.join(runtime, "cache/playwright");
export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 30000,
  outputDir: path.join(runtime, "test-runs/frontend/results"),
  reporter: [
    ["list"],
    [
      "html",
      {
        outputFolder: path.join(runtime, "test-runs/frontend/report"),
        open: "never",
      },
    ],
  ],
  use: {
    baseURL: "http://127.0.0.1:18721",
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : {},
  },
  webServer: {
    command: "npm run dev -- --port 18721 --strictPort",
    url: "http://127.0.0.1:18721",
    reuseExistingServer: false,
  },
});
