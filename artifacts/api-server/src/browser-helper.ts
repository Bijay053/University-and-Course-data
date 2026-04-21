import { normalizeScrapeUrl } from "./lib/normalize-scrape-url.js";

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
  /** HTML after opening important tabs (requirements/fees) and merging snapshots */
  requirementsHtml: string;
  /** List of interactions that succeeded */
  clicksPerformed: string[];
}

// ── Selector lists ────────────────────────────────────────────────────────────

const INTERNATIONAL_SELECTORS = [
  // Exact-text matchers FIRST so we hit the toggle radio/button, not body copy
  // that contains substrings like "an International Student".
  'input[type="radio"][value*="international" i]',
  'input[type="radio"][id*="international" i]',
  'label:text-is("International")',
  'button:text-is("International")',
  'a:text-is("International")',
  '[data-student-type="international"]',
  '[data-type="international"]',
  '[data-tab="international"]',
  '[data-value="international"]',
  '.international-toggle',
  '#international-btn',
  '[aria-label="International" i]',
  // Looser fallbacks
  'label:has-text("International")',
  'button:has-text("International")',
  'a:has-text("International")',
  'li:has-text("International students")',
];

const ON_CAMPUS_SELECTORS = [
  'button:has-text("On-campus")',
  'button:has-text("On campus")',
  'a:has-text("On-campus")',
  'a:has-text("On campus")',
  'label:has-text("On-campus")',
  'label:has-text("On campus")',
  '#on-campus',
  '#on_campus',
  '[value="on-campus"]',
  '[value="on campus"]',
  '[data-learning-mode="on-campus"]',
  '[data-learning-mode="on campus"]',
  '[data-mode="on-campus"]',
  '[data-mode="on campus"]',
  '[aria-label*="On-campus" i]',
  '[aria-label*="On campus" i]',
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

const FEES_TAB_SELECTORS = [
  'a:has-text("Fees and Scholarships")',
  'button:has-text("Fees and Scholarships")',
  'a:has-text("Fees & Scholarships")',
  'button:has-text("Fees & Scholarships")',
  '[href*="fees-and-scholarships"]',
  '[href*="fees"]',
  'li:has-text("Fees and Scholarships") a',
  'li:has-text("Fees and Scholarships") button',
];

const ACCORDION_PATTERNS = [
  "Minimum English Language Requirement",
  "English Language Requirements",
  "English Proficiency",
  "Language Requirements",
  "English Requirements",
];

function extractBodyInnerHtml(html: string): string {
  const match = html.match(/<body[^>]*>([\s\S]*)<\/body>/i);
  return match ? match[1] : html;
}

function mergeSnapshots(htmls: string[]): string {
  const seen = new Set<string>();
  const sections = htmls
    .map((html) => extractBodyInnerHtml(html).trim())
    .filter((body) => body.length > 0)
    .filter((body) => {
      if (seen.has(body)) return false;
      seen.add(body);
      return true;
    });

  return `<!doctype html><html><body>${sections.join("\n<!-- cursor-tab-snapshot -->\n")}</body></html>`;
}

// ── Core helper ───────────────────────────────────────────────────────────────

export async function fetchPageWithBrowser(
  url: string,
  opts: {
    clickInternational?: boolean;
    clickOnCampus?: boolean;
    clickRequirementsTab?: boolean;
    expandAccordions?: boolean;
    timeoutMs?: number;
  } = {},
): Promise<BrowserPageResult | null> {
  const {
    clickInternational = true,
    clickOnCampus = true,
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

    const snapshots: string[] = [];

    // ── Step 1: Click International toggle ───────────────────────────────────
    // Verify by COMPARING before vs after click: a real toggle changes the
    // visible content (e.g. injects a "Locations:" block, swaps the fee value).
    // A static body that always contains the word "International" must NOT
    // count as verified — that was the bug giving false positives on
    // VIT Diploma/Bachelor pages where the first click did nothing.
    const fingerprintInternational = async (): Promise<{ hasLocations: boolean; cityCount: number; bodyLen: number; activeIntl: boolean }> => {
      try {
        return await page.evaluate(() => {
          const body = (document.body?.innerText || "");
          const lower = body.toLowerCase();
          const hasLocations = /\blocations?\s*:\s*\n?\s*[a-z]/i.test(body) || /\bcampus(?:es)?\s*:/i.test(body);
          const cities = ["melbourne", "sydney", "adelaide", "brisbane", "perth", "canberra", "geelong", "gold coast", "hobart"];
          const cityCount = cities.reduce((n, c) => n + (lower.includes(c) ? 1 : 0), 0);
          let activeIntl = false;
          const active = document.querySelectorAll('[aria-selected="true"], .active, .selected, input:checked');
          for (const el of Array.from(active)) {
            const t = (el.textContent || "").trim().toLowerCase();
            const v = ((el as HTMLInputElement).value || "").toLowerCase();
            const id = (el.id || "").toLowerCase();
            if (t === "international" || t.startsWith("international ") || v.includes("international") || id.includes("international")) {
              activeIntl = true;
              break;
            }
            // Walk up: parent label may carry the text
            const parentText = (el.parentElement?.textContent || "").trim().toLowerCase();
            if (parentText === "international" || parentText.startsWith("international ")) { activeIntl = true; break; }
          }
          return { hasLocations, cityCount, bodyLen: body.length, activeIntl };
        });
      } catch { return { hasLocations: false, cityCount: 0, bodyLen: 0, activeIntl: false }; }
    };

    const renderedSinceBaseline = (
      baseline: { hasLocations: boolean; cityCount: number; bodyLen: number; activeIntl: boolean },
      now: { hasLocations: boolean; cityCount: number; bodyLen: number; activeIntl: boolean },
    ): boolean => {
      // Strongest signal: an "International" toggle is now active.
      if (now.activeIntl && !baseline.activeIntl) return true;
      // VIT-style: a Locations/Campus block appeared that wasn't visible before.
      if (now.hasLocations && !baseline.hasLocations) return true;
      // City count grew (international view added the campus list).
      if (now.cityCount >= baseline.cityCount + 2) return true;
      // Body text changed substantially (toggle injected a content panel).
      if (Math.abs(now.bodyLen - baseline.bodyLen) > 200) return true;
      return false;
    };

    if (clickInternational) {
      // First, gather every candidate via a scripted scan — much more reliable
      // than CSS :has-text on sites where "International" appears in body copy.
      type Candidate = { kind: string; ref: any };
      const scriptedCandidates = await page.evaluateHandle(() => {
        const out: Element[] = [];
        // 1) Radio inputs whose value/id mentions international
        document.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach((el) => {
          const v = ((el as HTMLInputElement).value || "").toLowerCase();
          const id = (el.id || "").toLowerCase();
          const name = ((el as HTMLInputElement).name || "").toLowerCase();
          if (v.includes("international") || id.includes("international") || name.includes("international")) out.push(el);
        });
        // 2) Elements whose direct text is exactly "International"
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
        let n: Node | null = walker.currentNode;
        while ((n = walker.nextNode())) {
          const el = n as HTMLElement;
          const own = Array.from(el.childNodes)
            .filter((c) => c.nodeType === 3)
            .map((c) => (c.textContent || "").trim())
            .join(" ")
            .trim();
          if (own.toLowerCase() === "international" && el.offsetParent !== null) {
            out.push(el);
          }
        }
        // De-duplicate while preserving order
        const seen = new Set<Element>();
        const uniq: Element[] = [];
        for (const el of out) { if (!seen.has(el)) { seen.add(el); uniq.push(el); } }
        return uniq;
      });

      const handles = await scriptedCandidates.evaluateHandle((arr) => arr).then(() => null).catch(() => null);
      // Iterate candidates via index (handles is unused; we re-enumerate below for click)
      const candidateCount: number = await page.evaluate((els: any) => (els as Element[]).length, scriptedCandidates).catch(() => 0);

      // Snapshot baseline BEFORE any click — verification compares against this.
      const baseline = await fingerprintInternational();

      let toggled = false;
      for (let i = 0; i < candidateCount; i++) {
        if (toggled) break;
        try {
          await page.evaluate(
            ({ els, idx }: any) => {
              const el = (els as Element[])[idx] as HTMLElement;
              if (!el) return;
              const target = (el as HTMLInputElement).type === "radio" || (el as HTMLInputElement).type === "checkbox"
                ? (el.closest("label") as HTMLElement) || el
                : el;
              target.click();
            },
            { els: scriptedCandidates, idx: i },
          );
          await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
          await page.waitForTimeout(1200);
          const now = await fingerprintInternational();
          if (renderedSinceBaseline(baseline, now)) {
            toggled = true;
            clicksPerformed.push(`international_toggle_scripted[${i}]`);
            break;
          }
        } catch { /* try next */ }
      }
      void handles;

      // Fallback to original CSS selector list
      if (!toggled) {
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
            await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
            await page.waitForTimeout(1200);
            const now = await fingerprintInternational();
            if (renderedSinceBaseline(baseline, now)) {
              toggled = true;
              clicksPerformed.push(`international_toggle_css[${sel.slice(0, 40)}]`);
              break;
            }
          } catch { /* try next */ }
        }
      }

      if (!toggled) {
        clicksPerformed.push("international_toggle_NOT_VERIFIED");
      }
    }

    // ── Step 1b: Click On-campus / On campus mode ────────────────────────────
    if (clickOnCampus) {
      for (const sel of ON_CAMPUS_SELECTORS) {
        try {
          const el = await page.$(sel);
          if (!el) continue;
          const visible = await el.isVisible();
          if (!visible) continue;
          const classes = (await el.getAttribute("class")) || "";
          const ariaSelected = await el.getAttribute("aria-selected");
          const checked = await el.getAttribute("checked");
          const alreadyActive =
            classes.includes("active") || classes.includes("selected") || ariaSelected === "true" || checked != null;
          if (!alreadyActive) await el.click();
          await page.waitForTimeout(2000);
          clicksPerformed.push("on_campus_toggle");
          break;
        } catch {
          // try next selector
        }
      }
    }

    const mainHtml = await page.content();
    snapshots.push(mainHtml);

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
              const tag = await el.evaluate((n: { tagName: string }) => n.tagName.toLowerCase());
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
      snapshots.push(requirementsHtml);
    }

    // ── Step 4: Click Fees tab (for fee schedule links / hidden fee content) ──
    for (const sel of FEES_TAB_SELECTORS) {
      try {
        const el = await page.$(sel);
        if (!el) continue;
        const visible = await el.isVisible();
        if (!visible) continue;
        await el.click();
        await page.waitForTimeout(1500);
        clicksPerformed.push("fees_tab");
        snapshots.push(await page.content());
        break;
      } catch {
        // try next selector
      }
    }

    // ── Step 5: Click English-test equivalence modals/links ─────────────────
    // Many universities (VIT, KOI, ASA, Torrens, Newcastle, etc.) hide
    // PTE/TOEFL/CAE/Duolingo behind a link/button such as
    //   "another approved English language test"
    //   "equivalent English test scores"
    //   "accepted English language tests"
    // that opens a modal or expands an inline panel. The modal table contains
    // the full equivalence row, so we must trigger it before snapshotting.
    try {
      const ENGLISH_MODAL_PATTERNS_SRC = [
        "another approved english",
        "equivalent english(?:\\s+language)?\\s+test",
        "english (?:test )?(?:score )?equivalen",
        "accepted english (?:language\\s+)?tests?",
        "alternative english (?:language\\s+)?tests?",
        "english language (?:test\\s+)?scores?",
        "english proficiency (?:tests?|requirements?)",
        "view (?:full )?english (?:test\\s+)?(?:requirements?|scores?)",
      ];
      const triggerHandles = await page.evaluateHandle((patternStrs: string[]) => {
        const patterns = patternStrs.map((s) => new RegExp(s, "i"));
        const out: Element[] = [];
        const sel = "a, button, span, summary, div[role='button'], [data-toggle='modal'], [data-bs-toggle='modal'], [data-target], [data-bs-target]";
        document.querySelectorAll(sel).forEach((el) => {
          const txt = (el.textContent || "").trim().slice(0, 250);
          if (txt && patterns.some((p) => p.test(txt))) out.push(el);
        });
        return out;
      }, ENGLISH_MODAL_PATTERNS_SRC);
      const triggerCount: number = await page.evaluate(
        (els: any) => (els as Element[]).length,
        triggerHandles,
      ).catch(() => 0);
      let modalsOpened = 0;
      for (let i = 0; i < Math.min(triggerCount, 4); i++) {
        try {
          await page.evaluate(
            ({ els, idx }: any) => {
              const el = (els as Element[])[idx] as HTMLElement;
              if (!el) return;
              try { el.scrollIntoView({ block: "center" }); } catch {}
              el.click();
            },
            { els: triggerHandles, idx: i },
          );
          await page.waitForTimeout(1500);
          snapshots.push(await page.content());
          modalsOpened++;
          // Try to close any open modal so the next click isn't blocked
          try {
            await page.evaluate(() => {
              const closeSel = "[data-dismiss='modal'], [data-bs-dismiss='modal'], .modal.show .close, .modal.show button[aria-label='Close']";
              const btn = document.querySelector(closeSel) as HTMLElement | null;
              if (btn) btn.click();
            });
            await page.waitForTimeout(400);
          } catch {}
        } catch { /* try next */ }
      }
      if (modalsOpened > 0) clicksPerformed.push(`english_modals_${modalsOpened}`);
    } catch { /* ignore */ }

    requirementsHtml = mergeSnapshots(snapshots);

    await browser.close();
    return { mainHtml, requirementsHtml, clicksPerformed };
  } catch (err) {
    if (browser) await browser.close().catch(() => {});
    return null;
  }
}

// ── Domain/URL heuristic: is this site likely JS-interactive? ────────────────

const JS_HEAVY_DOMAINS = [
  "asahe.edu.au",
  "vit.edu.au",
  "victorianinstitute.edu.au",
  "newcastle.edu.au",
  "rmit.edu.au",
  "uts.edu.au",
  "koi.edu.au",
];

export function siteNeedsBrowser(url: string): boolean {
  try {
    const host = new URL(normalizeScrapeUrl(url)).hostname.toLowerCase();
    return JS_HEAVY_DOMAINS.some((d) => host === d || host.endsWith("." + d));
  } catch {
    return false;
  }
}
