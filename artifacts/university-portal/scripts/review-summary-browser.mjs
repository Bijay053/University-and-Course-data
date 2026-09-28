// Browser-only synthetic API test. Run against an already-running portal:
// PORTAL_URL=http://localhost:80 node artifacts/university-portal/scripts/review-summary-browser.mjs
// No real API requests, credentials, or mutations are permitted.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { performance } from "node:perf_hooks";
import { chromium } from "../../api-server/node_modules/playwright/index.mjs";

const base = process.env.PORTAL_URL || "http://localhost:80";
const job = "synthetic-review-a";
const otherJob = "synthetic-review-b";
const snippet = "COMPLETE SYNTHETIC SOURCE SNIPPET " + "verified fee evidence ".repeat(180);
const rows = Array.from({ length: 100 }, (_, i) => ({
  id: i + 1, scrapeJobId: job, universityId: 7, courseName: `Synthetic Course ${i + 1}`,
  courseWebsite: `https://example.test/course/${i + 1}`, status: "pending", category: "Business",
  degreeLevel: "Master", duration: 2, durationTerm: "Year", courseLocation: "Sydney",
  studyMode: "On Campus", internationalFee: 30000, currency: "AUD", feeTerm: "Annual",
  completeness: 80, intakeMonths: ["February"], scrapeWarnings: [],
  evidenceCount: 1, evidenceLoaded: false,
}));
const fullRows = rows.map((course) => ({
  ...course, subCategory: "Hydrated from complete course detail", evidenceLoaded: true,
  evidence: [{ id: course.id + 1000, fieldKey: "international_fee",
    candidateValue: "30000", normalizedValue: "30000", selected: true,
    sourceUrl: course.courseWebsite, pageType: "html", extractionMethod: "html",
    snippet, confidence: 0.97 }],
  raw_data: "synthetic original source material ".repeat(350),
  extraction_method: { source_text: "synthetic extraction trace ".repeat(260) },
}));
const jsonBytes = (value) => Buffer.byteLength(JSON.stringify(value));
const summaryBody = { courses: rows };
const fullBody = { courses: fullRows };
const detail = (id, forJob = job) => ({
  course: { ...fullRows[id - 1], scrapeJobId: forJob },
});
const historyRun = (runtimeJobId, name, count) => ({
  runtimeJobId, universityId: 7, universityName: name,
  status: "completed", stagedCount: count, approvedCount: 0, rejectedCount: 0,
  totalFound: count, imported: count, skipped: 0, errors: 0,
  startedAt: "2026-09-01T00:00:00Z", completedAt: "2026-09-01T00:00:01Z",
  releaseHistory: [], releaseWarnings: [],
});
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const browser = await chromium.launch({
  headless: true, executablePath: process.env.CHROMIUM_PATH || "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium",
  args: ["--no-sandbox"],
});

async function scenario(mode) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript((savedJob) => {
    sessionStorage.setItem("scrape_slot_0_jobId", savedJob);
  }, job);
  const page = await context.newPage();
  const requests = [];
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (entry) => {
    if (entry.type() === "error") browserErrors.push(entry.text());
  });
  let failDetail = false;
  let detailDelay = 40;
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    requests.push(`${req.method()} ${path}${url.search}`);
    if (req.method() !== "GET") {
      await route.fulfill({ status: 405, body: "Synthetic browser test blocks all writes" });
      throw new Error(`Unexpected API write: ${req.method()} ${path}`);
    }
    let body = {};
    let status = 200;
    if (path === "/api/auth/me") {
      body = { user: { id: 1, name: "Synthetic Reviewer", email: "reviewer@example.test", role: "admin" },
        permissions: [], is_super_admin: true };
    } else if (path === `/api/scrape/status/${job}`) {
      body = { status: "completed", universityId: 7, universityName: "Synthetic University",
        reviewableCount: 100, imported: 100, skipped: 0, errors: 0, logs: [] };
    } else if (path === `/api/scrape/staged/${job}`) {
      // Force full data even for ?view=summary in the simulated baseline,
      // while still asserting the current client requests the summary URL.
      body = mode === "full" ? fullBody : summaryBody;
    } else if (path === `/api/scrape/staged/${otherJob}`) {
      body = { courses: [{ ...rows[0], id: 201, scrapeJobId: otherJob, courseName: "Other Job Course" }] };
    } else if (/^\/api\/scrape\/staged\/\d+\/evidence$/.test(path)) {
      await wait(detailDelay);
      status = failDetail ? 503 : 200;
      body = failDetail ? { detail: "Synthetic source temporarily unavailable" }
        : detail(Number(path.split("/")[4]), url.searchParams.get("jobId") || job);
    } else if (path === "/api/scrape/history") {
      body = { runs: [
        historyRun(job, "Synthetic University", 100),
        historyRun(otherJob, "Second Synthetic University", 1),
      ], total: 2 };
    } else if (path === `/api/scrape/history/${job}`) {
      body = { logs: [], stagedCourses: rows, unresolvedCourses: [] };
    } else if (path === `/api/scrape/history/${otherJob}`) {
      body = { logs: [], stagedCourses: [{
        ...rows[0], id: 201, courseName: "Other Job Course", scrapeJobId: otherJob,
      }], unresolvedCourses: [] };
    } else if (path.endsWith("/quality-actions")) {
      body = { job_id: job, current_avg_completeness: 80, last_run: null,
        performance: { jobs_in_gap: 0, jobs_above_threshold: 0,
          pushed_above_threshold: false, completeness_gain_pct: 0 } };
    } else if (path === "/api/import/history") body = [];
    else if (path.endsWith("/course-quality")) body = { courses: [] };
    else if (path.startsWith("/api/courses")) body = { total: 0, courses: [] };
    // Use a fixed 40 ms synthetic network latency to make timings comparable.
    await wait(40);
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  });

  try {
    const start = performance.now();
    await page.goto(new URL("/scraping", base).href, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.getByTestId("text-review-entry-count").filter({ hasText: "100 pending entries" }).waitFor({ timeout: 30000 });
    const initialMs = Math.round(performance.now() - start);
    const stagedCalls = requests.filter((r) => r.includes(`/api/scrape/staged/${job}`) && !r.includes("/evidence"));
    assert(stagedCalls.length >= 2 && stagedCalls.every((r) => r.includes("view=summary")),
      `All staged count/list requests must use view=summary: ${stagedCalls.join(", ")}`);
    assert(!requests.some((r) => /\/evidence\?/.test(r)), "No per-course evidence request before Sources");
    if (mode === "summary") {
      const screenshotPath = "artifacts/university-portal/scripts/screenshots/review-summary.png";
      await mkdir("artifacts/university-portal/scripts/screenshots", { recursive: true });
      await page.getByTestId("text-review-entry-count").scrollIntoViewIfNeeded();
      await page.screenshot({ path: screenshotPath });
      console.log(`Saved Review screenshot: ${screenshotPath}`);
    }

    // The operator's historical View Courses table is the Sources-capable Review table.
    await page.locator(`#scrape-history-${job}`).getByRole("button", { name: "View Courses" }).click();
    const toggle = page.getByTestId("sources-toggle-1");
    await toggle.waitFor({ timeout: 15000 });
    const historyCalls = requests.filter((r) => /^GET \/api\/scrape\/history\/synthetic-review-/.test(r));
    assert(historyCalls.length > 0 && historyCalls.every((r) => r.includes("view=summary")),
      `History detail must always request summaries: ${historyCalls.join(", ")}`);
    const sourceStart = performance.now();
    await toggle.click();
    // The evidence UI line-clamps visually but keeps the entire snippet in
    // both its DOM text and title (rather than cutting it in the API payload).
    const snippetElement = page.locator(`[title="${snippet}"]`).first();
    await snippetElement.waitFor({ timeout: 15000 });
    assert.equal(await snippetElement.getAttribute("title"), snippet);
    const sourcesMs = Math.round(performance.now() - sourceStart);
    assert(requests.some((r) => r.includes("/api/scrape/staged/1/evidence?") && r.includes(`jobId=${job}`)),
      "Sources must load fenced detail on demand");

    // Force an explicit server error, then retry successfully without changing the course.
    await page.getByTestId("sources-toggle-2").click();
    failDetail = true;
    await page.getByTestId("sources-toggle-3").click();
    try {
      await page.getByTestId("sources-error-3").waitFor({ timeout: 15000 });
    } catch (error) {
      console.error("Retry diagnostic:", {
        evidenceRequests: requests.filter((r) => r.includes("/evidence")),
        row3: (await page.getByTestId("sources-toggle-3").locator("xpath=ancestor::tr").innerText()).slice(0, 250),
        alertText: await page.locator('[role="alert"]').allInnerTexts(),
      });
      throw error;
    }
    assert.match(await page.getByTestId("sources-error-3").innerText(), /Synthetic source temporarily unavailable/);
    failDetail = false;
    await page.getByTestId("sources-retry-3").click();
    await page.getByTestId("sources-error-3").waitFor({ state: "detached", timeout: 15000 });
    assert((await page.getByTestId("sources-toggle-3").count()) === 1);

    // The main editable Review row must hydrate a complete detail before opening
    // the form; never save a summary. Read-only verification, no PUT is sent.
    await page.getByTestId("edit-staged-course-1").click();
    const editDialog = page.getByRole("dialog", { name: "Edit Scraped Course" });
    await editDialog.waitFor({ timeout: 15000 });
    assert.equal(await editDialog.locator("label").filter({ hasText: "Sub Category" })
      .locator("xpath=following-sibling::input").inputValue(),
      "Hydrated from complete course detail");
    await page.keyboard.press("Escape");
    await editDialog.waitFor({ state: "detached", timeout: 15000 });

    // A slow detail from job A must not leak into job B after switching history jobs.
    detailDelay = 500;
    await page.evaluate(() => document.querySelector('[data-testid="sources-toggle-4"]')?.click());
    await page.locator(`#scrape-history-${otherJob}`).getByRole("button", { name: "View Courses" }).click();
    await page.getByText("Other Job Course").first().waitFor({ timeout: 15000 });
    await wait(600);
    assert.equal(await page.getByTestId("sources-toggle-4").count(), 0, "Switched job must discard in-flight detail");
    assert.equal(await page.locator(`[title="${snippet}"]`).count(), 0, "Old snippet must not appear in the new job");
    const allHistoryCalls = requests.filter((r) => /^GET \/api\/scrape\/history\/synthetic-review-/.test(r));
    assert(allHistoryCalls.every((r) => r.includes("view=summary")), "Every history detail must use summary");
    return { mode, initialMs, sourcesMs, stagedCalls, requests: requests.length };
  } catch (error) {
    console.error("Browser diagnostic:", {
      mode, url: page.url(), historyCards: await page.locator('[id^="scrape-history-"]').count(),
      rowToggles: await page.locator('[data-testid^="sources-toggle-"]').count(),
      recentRequests: requests.slice(-16),
      browserErrors,
      errors: await page.locator('[role="alert"]').allInnerTexts(),
      bodyTail: (await page.locator("body").innerText()).slice(-500),
    });
    throw error;
  } finally {
    await context.close();
  }
}

try {
  const full = await scenario("full");
  const summary = await scenario("summary");
  const fullBytes = jsonBytes(fullBody);
  const summaryBytes = jsonBytes(summaryBody);
  assert(fullBytes > summaryBytes, "Full fixture must be larger than summary fixture");
  console.log(JSON.stringify({
    payload: { fullBytes, summaryBytes, reductionPct: Number(((1 - summaryBytes / fullBytes) * 100).toFixed(1)) },
    browser: { full, summary },
    distinction: "Browser timings are intercepted synthetic-network timings, not a real HTTP/database backend benchmark.",
  }, null, 2));
} finally {
  await browser.close();
}