// Synthetic, read-only browser geometry regression. See course-toolbar-browser.md.
import assert from "node:assert/strict";
import { access, mkdir } from "node:fs/promises";
import { chromium } from "../../api-server/node_modules/playwright/index.mjs";

const base = new URL(process.env.PORTAL_URL || "http://localhost:80");
const output = new URL("../../../generated-artifacts/course-toolbar/", import.meta.url).pathname;
const widths = [390, 640, 768, 1024, 1280, 1366, 1440];
const tolerance = 2; // Allow subpixel rounding, not another line of controls.
const course = {
  id: 1, course_name: "Master of Data Science",
  university: { id: 9, name: "Harbour University", city: "Sydney", country: "Australia", logo_url: null },
  course_location: "Sydney", offerings: [], degree_level: "Master", category: null,
  duration: 2, duration_term: "Year", intakes: ["February"],
  international_fee: 41200, currency: "AUD", fee_term: "Year", fee_year: 2026,
  english_requirements: { ielts_overall: 6.5 }, course_url: "https://example.test/course",
};
const options = {
  countries: ["Australia"], universities: [{ id: 9, name: "Harbour University" }],
  degree_levels: ["Master"], intake_months: ["February"], currencies: ["AUD", "GBP"],
  qualifications: [], grading_schemes: [], english_exams: ["IELTS"],
};

// Match the existing review-summary-browser convention, but resolve the system
// browser from PATH rather than pinning a particular Nix store revision.
const systemChromium = "/usr/bin/chromium";
let executablePath = process.env.CHROMIUM_PATH;
if (!executablePath) {
  for (const dir of (process.env.PATH || "").split(":")) {
    try {
      await access(`${dir}/chromium`);
      executablePath = `${dir}/chromium`;
      break;
    } catch { /* Try the next PATH entry. */ }
  }
}
assert(executablePath, `Chromium not found; set CHROMIUM_PATH (for example ${systemChromium})`);
const browser = await chromium.launch({ headless: true, executablePath, args: ["--no-sandbox"] });
await mkdir(output, { recursive: true });

async function geometry(page) {
  return page.evaluate(() => {
    const rect = (el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const byId = (id) => document.querySelector(`[data-testid="${id}"]`);
    const header = byId("course-results-header");
    const toolbar = byId("course-results-toolbar");
    const controls = [
      byId("button-open-filters"), byId("select-sort"), byId("select-sort-currency"),
      toolbar.querySelector('[role="group"]'),
    ].filter((el) => el.getBoundingClientRect().width > 0).map(rect);
    return {
      viewport: document.documentElement.clientWidth,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      header: rect(header), toolbar: rect(toolbar),
      heading: rect(document.getElementById("results-heading")),
      count: rect(byId("text-result-count")), controls,
      help: document.getElementById("fee-sort-help") ? rect(document.getElementById("fee-sort-help")) : null,
    };
  });
}

function check(g, width, state) {
  const label = `${width}px / ${state}`;
  const require = (condition, message) => assert(condition, `${label}: ${message}\n${JSON.stringify(g, null, 2)}`);
  require(g.scrollWidth <= g.viewport + tolerance, "page must not overflow horizontally");
  for (const [name, r] of [
    ["header", g.header], ["heading", g.heading], ["count", g.count],
    ["toolbar", g.toolbar], ...g.controls.map((r, i) => [`control ${i}`, r]),
    ...(g.help ? [["currency help", g.help]] : []),
  ]) {
    require(r.width > 0 && r.height > 0, `${name} must have a visible box`);
    require(r.x >= -tolerance && r.right <= g.viewport + tolerance, `${name} must fit the viewport`);
    require(r.x >= g.header.x - tolerance && r.right <= g.header.right + tolerance,
      `${name} must stay inside the results header`);
    require(r.y >= g.header.y - tolerance && r.bottom <= g.header.bottom + tolerance,
      `${name} must stay inside the results header vertically`);
  }
  require(g.count.y >= g.heading.bottom - tolerance, "count must remain below the heading");
  if (width >= 640) {
    require(Math.abs(g.heading.y - g.toolbar.y) <= tolerance, "toolbar must start beside Courses, not wrap below it");
    require(g.heading.right <= g.toolbar.x + tolerance, "heading and toolbar must not overlap");
    const centers = g.controls.map((r) => r.y + r.height / 2);
    require(Math.max(...centers) - Math.min(...centers) <= tolerance, "all controls must share one row");
  } else {
    require(g.toolbar.y >= g.count.bottom - tolerance, "phone toolbar must sit below the heading/count");
  }
  // Mobile may wrap between controls, but controls must never overlap.
  for (let i = 0; i < g.controls.length; i++) {
    for (const b of g.controls.slice(i + 1)) {
      const a = g.controls[i];
      require(a.right <= b.x + tolerance || b.right <= a.x + tolerance ||
        a.bottom <= b.y + tolerance || b.bottom <= a.y + tolerance, "controls must not overlap");
    }
  }
  require(Boolean(g.help) === (state === "help-visible"), "currency-help visibility must match the selected currency");
  if (g.help) require(g.help.y >= Math.max(...g.controls.map((r) => r.bottom)) - tolerance,
    "currency help must stay below the controls");
}

// Prove these assertions reject real browser regressions, not merely a passing
// baseline. Styles are injected into the test page only and always removed.
async function proveGuard(page, width, css, expectedMessage) {
  const style = await page.addStyleTag({ content: css });
  try {
    const g = await geometry(page);
    assert.throws(() => check(g, width, "help-visible"), expectedMessage);
  } finally {
    await style.evaluate((el) => el.remove());
  }
}

try {
  for (const width of widths) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, serviceWorkers: "block" });
    const page = await context.newPage();
    const errors = [];
    let courseRequests = 0;
    page.on("pageerror", (error) => errors.push(error.message));
    // Fail closed: no API request reaches the backend and no write is allowed.
    await page.route("**/api/**", async (route) => {
      const req = route.request();
      const path = new URL(req.url()).pathname;
      let body;
      let status = 200;
      if (req.method() !== "GET") {
        errors.push(`Unexpected API write: ${req.method()} ${path}`);
        status = 405;
        body = {};
      } else if (path === "/api/auth/me") body = { user: null };
      else if (path === "/api/search/options") body = options;
      else if (path === "/api/search/courses") {
        courseRequests++;
        body = { total: 1, page: 1, limit: 12, results: [course] };
      } else {
        errors.push(`Unexpected API request: ${path}`);
        status = 404;
        body = {};
      }
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    });
    try {
      await page.goto(new URL("/toolkit", base).href, { waitUntil: "domcontentloaded" });
      await page.getByTestId("card-course-1").waitFor();
      await page.waitForFunction(() => !document.querySelector('[data-testid="select-sort-currency"]').disabled);
      await page.evaluate(() => document.fonts.ready);
      for (const state of ["help-visible", "help-hidden", "help-restored"]) {
        const visible = state !== "help-hidden";
        if (state !== "help-visible") {
          await page.getByTestId("select-sort-currency").selectOption(visible ? "" : "AUD");
        }
        await page.waitForFunction((show) => Boolean(document.getElementById("fee-sort-help")) === show, visible);
        const sort = page.getByTestId("select-sort");
        assert.equal(await sort.getAttribute("aria-describedby"), visible ? "fee-sort-help" : null);
        check(await geometry(page), width, visible ? "help-visible" : "help-hidden");
        await page.screenshot({ path: `${output}/${width}-${state}.png`, fullPage: true });
        console.log(`PASS ${width}px ${state}`);
      }
      if (width === 1366) {
        await proveGuard(page, width,
          '[data-testid="course-results-header"] { grid-template-columns: minmax(0, 1fr) !important; }',
          /toolbar must start beside Courses/);
        await proveGuard(page, width,
          '[data-testid="course-results-toolbar"] > div { flex-direction: column !important; }',
          /all controls must share one row/);
        console.log("PASS guards reject heading and control wrapping");
      }
      if (width === 390) {
        await proveGuard(page, width,
          '[data-testid="course-results-header"] { width: 900px !important; }',
          /overflow horizontally|fit the viewport/);
        console.log("PASS guard rejects phone overflow");
      }
      assert(courseRequests > 0, "Synthetic courses must have been requested");
      assert.deepEqual(errors, [], "No browser errors or unexpected API requests");
    } catch (error) {
      await page.screenshot({ path: `${output}/${width}-failure.png`, fullPage: true }).catch(() => {});
      throw error;
    } finally {
      await context.close();
    }
  }
  console.log(`Course toolbar layout passed at all ${widths.length} widths. Screenshots: ${output}`);
} finally {
  await browser.close();
}
