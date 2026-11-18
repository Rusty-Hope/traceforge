/**
 * Screenshot generator for the README (puppeteer-core + system Chrome).
 *
 *   node scripts/screenshot.mjs [baseUrl]
 *
 * Waits for real data (triaged incident rows + recharts SVGs) before capturing,
 * so the shots showcase live ML detection + background LLM triage.
 */
import puppeteer from "puppeteer-core";
import { existsSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = join(root, "..", "docs", "screenshots");
const BASE = process.argv[2] ?? "http://localhost:8000";

const CHROME_PATHS = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium-browser",
];

const executablePath = CHROME_PATHS.find((p) => existsSync(p));
if (!executablePath) throw new Error("no Chrome/Chromium found");

const browser = await puppeteer.launch({
  executablePath,
  headless: "new",
  args: ["--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2"],
});

const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 2 });

const waitData = async () => {
  await page.waitForFunction(
    () => document.querySelectorAll("tbody tr[data-incident-id]").length > 0,
    { timeout: 60_000 },
  );
  // Wait for charts to render and at least one incident to finish triage when LLM is enabled
  await page.waitForFunction(
    () => document.querySelectorAll("svg.recharts-surface").length >= 2,
    { timeout: 60_000 },
  );
  await page.waitForFunction(
    () => [...document.querySelectorAll("tbody tr")].some((r) => r.textContent?.includes("TRIAGED")),
    { timeout: 30_000 },
  ).catch(() => {});
  await new Promise((r) => setTimeout(r, 1200));
};

mkdirSync(outDir, { recursive: true });

// 1 — dashboard overview
await page.goto(`${BASE}/`, { waitUntil: "networkidle2", timeout: 60_000 });
await waitData();
await page.screenshot({ path: join(outDir, "dashboard.png") });
console.log("dashboard.png ✓");

// 2 — incident detail with ML evidence, blast radius, and LLM triage
const targetId = await page.evaluate(() => {
  const rows = [...document.querySelectorAll("tbody tr[data-incident-id]")];
  const triaged = rows.find((r) => r.textContent?.includes("TRIAGED") && r.textContent?.includes("pool"));
  const anyTriaged = rows.find((r) => r.textContent?.includes("TRIAGED"));
  return (triaged ?? anyTriaged ?? rows[0])?.getAttribute("data-incident-id") ?? "1";
});
await page.goto(`${BASE}/?incident=${targetId}`, { waitUntil: "networkidle2", timeout: 60_000 });
await page.waitForFunction(
  () => document.body.innerText.includes("INCIDENT #") && document.body.innerText.includes("RAW SAMPLES"),
  { timeout: 30_000 },
);
await new Promise((r) => setTimeout(r, 1200));
await page.screenshot({ path: join(outDir, "incident-detail.png") });
console.log(`incident-detail.png ✓ (incident #${targetId})`);

await browser.close();
