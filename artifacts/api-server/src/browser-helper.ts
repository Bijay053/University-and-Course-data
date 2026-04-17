/**
 * Minimal browser automation helper.
 *
 * Responsibility: ONLY click interactive elements (International toggle,
 * Entry Requirements tab, accordions), then return the final HTML to the
 * existing static extractors — no extraction logic here.
 *
 * Playwright is loaded lazily so the module never crashes when Chromium is
 * absent; the caller receives null and falls back to plain fetchPage.
 */

export interface BrowserPageResult {
  /** HTML after clicking International toggle */
  mainHtml: string;
  /** HTML after also opening the Entry Requirements tab (may equal mainHtml) */
  requirementsHtml: string;
  /** List of interactions that succeeded */
  clicksPerformed: string[];
}

// ── Selector lists ────────────────────────────────────────────────────────────

const INTERNATIONAL_SELECTORS = [
  'button:has-text("International")',
  'a:has-text("International")',
  'label:has-text("International")',
  'li:has-text("International students")',
  '[data-student-type="international"]',
  '[data-type="international"]',
  '.international-toggle',
  '#international-btn',
  '[aria-label*="International" i]',
];

const REQUIREMENTS_TAB_SELECTORS = [
  'a:has-text("Entry Requirements")',
  'button:has-text("Entry Requirements")',
  '[href*="entry-requirements"]',
  '[href*="entryrequirements"]',
  'a:has-text("Admission Requirements")',
  'li:has-text("Entry Requirements") a',
  'li:has-text("Entry Requirements") button',
];

const ACCORDION_PATTERNS = [
  "Minimum English Language Requirement",
  "English Language Requirements",
  "English Proficiency",
  "Language Requirements",
  "English Requirements",
];

// ── Core helper ───────────────────────────────────────────────────────────────

export async function fetchPageWithBrowser(
  url: string,
  opts: {
    clickInternational?: boolean;
    clickRequirementsTab?: boolean;
    expandAccordions?: boolean;
    timeoutMs?: number;
  } = {},
): Promise<BrowserPageResult | null> {
  const {
    clickInternational = true,
    clickRequirementsTab = true,
    expandAccordions = true,
    timeoutMs = 30_000,
  } = opts;

  let playwright: typeof import("playwright") | null = null;
  try {
    playwright = await import("playwright");
  } catch {
    return null; // playwright not available
  }

  // Prefer the system-managed Chromium (installed via Nix) over the Playwright-
  // managed binary, because the Playwright binary needs system libs not present
  // in this container environment.
  let executablePath: string | undefined;
  try {
    const { execSync } = await import("child_process");
    const found = execSync("which chromium 2>/dev/null || which chromium-browser 2>/dev/null || true")
      .toString().trim();
    if (found) executablePath = found;
  } catch { /* use Playwright default */ }

  const clicksPerformed: string[] = [];
  let browser: import("playwright").Browser | null = null;

  try {
    browser = await playwright.chromium.launch({
      headless: true,
      executablePath,
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-zygote",
      ],
    });
    const context = await browser.newContext({
      userAgent:
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
      locale: "en-US",
    });
    const page = await context.newPage();

    await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeoutMs });
    // Extra settle time for JS widgets
    await page.waitForTimeout(2000);

    // ── Step 1: Click International toggle ───────────────────────────────────
    if (clickInternational) {
      for (const sel of INTERNATIONAL_SELECTORS) {
        try {
          const el = await page.$(sel);
          if (!el) continue;
          const visible = await el.isVisible();
          if (!visible) continue;
          const classes = (await el.getAttribute("class")) || "";
          const ariaSelected = await el.getAttribute("aria-selected");
          const alreadyActive =
            classes.includes("active") || classes.includes("selected") || ariaSelected === "true";
          if (!alreadyActive) await el.click();
          await page.waitForTimeout(2000);
          clicksPerformed.push("international_toggle");
          break;
        } catch {
          // try next selector
        }
      }
    }

    const mainHtml = await page.content();

    // ── Step 2: Click Entry Requirements tab ─────────────────────────────────
    let requirementsHtml = mainHtml;

    if (clickRequirementsTab) {
      let tabClicked = false;
      for (const sel of REQUIREMENTS_TAB_SELECTORS) {
        try {
          const el = await page.$(sel);
          if (!el) continue;
          const visible = await el.isVisible();
          if (!visible) continue;
          await el.click();
          await page.waitForTimeout(1500);
          tabClicked = true;
          clicksPerformed.push("requirements_tab");
          break;
        } catch {
          // try next selector
        }
      }

      // ── Step 3: Expand accordion sections ──────────────────────────────────
      if (tabClicked && expandAccordions) {
        let expanded = 0;
        for (const pattern of ACCORDION_PATTERNS) {
          try {
            const candidates = await page.$$(`text="${pattern}"`);
            for (const el of candidates) {
              if (!(await el.isVisible())) continue;
              const tag = await el.evaluate((n: Element) => n.tagName.toLowerCase());
              if (["button", "a", "div", "summary", "span", "li"].includes(tag)) {
                await el.click();
                await page.waitForTimeout(600);
                expanded++;
              }
            }
          } catch {
            // continue
          }
        }
        if (expanded > 0) clicksPerformed.push(`accordions_expanded_${expanded}`);
      }

      requirementsHtml = await page.content();
    }

    await browser.close();
    return { mainHtml, requirementsHtml, clicksPerformed };
  } catch (err) {
    if (browser) await browser.close().catch(() => {});
    return null;
  }
}

// ── Domain/URL heuristic: is this site likely JS-interactive? ────────────────

const JS_HEAVY_DOMAINS = [
  "vit.edu.au",
  "victorianinstitute.edu.au",
  "newcastle.edu.au",
  "rmit.edu.au",
  "uts.edu.au",
];

export function siteNeedsBrowser(url: string): boolean {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return JS_HEAVY_DOMAINS.some((d) => host === d || host.endsWith("." + d));
  } catch {
    return false;
  }
}
