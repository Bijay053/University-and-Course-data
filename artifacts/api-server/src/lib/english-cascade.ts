/**
 * Universal English Requirements Cascade
 *
 * One safety-net function called from BOTH scrapeCourseBatch and runNoAiScrapeJob
 * to fill missing IELTS / PTE / TOEFL / CAE values when the regular extractors
 * have come up empty.
 *
 * Cascade order (each step short-circuits as soon as all 4 fields are filled):
 *   1. Plain text scan of the rendered HTML body
 *   2. Click ALL english/entry/admission/requirement tabs, re-scan
 *   3. Click ALL equivalence/test modal triggers, re-scan
 *   4. Vision-AI on candidate <img>s + a full-page screenshot fallback
 *   5. Follow english/admission/requirement in-page links, re-scan
 *
 * The function MUTATES the supplied `course` object in place (only fills empty
 * slots — never overwrites existing values) and returns:
 *   - steps:    a log of which strategies ran / hit
 *   - evidence: ReviewSource-shaped objects to push onto reviewSources
 *
 * Designed to be cheap when called: returns immediately if the course already
 * has IELTS+PTE+TOEFL+CAE, and never throws — all failures are swallowed.
 */

import * as cheerio from "cheerio";
import {
  parseEnglishRequirementsFromText,
  applyEnglishResultToCourse,
  hasEnglishTestKeyword,
  type EnglishCourseFields,
} from "./english-requirements.js";

// ── Gemini client (shared retry + model-fallback) ─────────────────────────────
import { callGeminiWithModelFallback } from "./gemini-client.js";

const GEMINI_API_KEY = process.env.GEMINI_API_KEY;

// ── Selector patterns ────────────────────────────────────────────────────────

const TAB_PATTERNS = [
  "Entry Requirements",
  "Admission Requirements",
  "Academic Requirements",
  "English Requirements",
  "English Language Requirements",
  "Language Requirements",
  "International Requirements",
  "Requirements",
];

const MODAL_PATTERNS = [
  "Equivalent",
  "Another approved English test",
  "View English test scores",
  "English Test Equivalencies",
  "Approved English test",
  "Other English tests",
  "Show English requirements",
  "View Requirements",
];

// ── Types ────────────────────────────────────────────────────────────────────

export type CascadeEvidence = {
  url: string;
  pageType: string;
  extractionMethod: string;
  content: string;
};

export type CascadeOutcome = {
  steps: string[];
  evidence: CascadeEvidence[];
};

// ── Concurrency control ──────────────────────────────────────────────────────
//
// The cascade launches its own Chromium instance, so under high parallelism
// (scrapeCourseBatch can run 32 courses concurrently) we'd otherwise spawn
// 32 browsers — guaranteed OOM / timeout collapse on JS-heavy sites. Cap at
// 3 concurrent cascade runs; everyone else waits.

const MAX_CONCURRENT_CASCADES = 3;
let activeCascades = 0;
const cascadeWaiters: Array<() => void> = [];

async function acquireCascadeSlot(): Promise<void> {
  if (activeCascades < MAX_CONCURRENT_CASCADES) {
    activeCascades++;
    return;
  }
  await new Promise<void>((resolve) => cascadeWaiters.push(resolve));
  activeCascades++;
}

function releaseCascadeSlot(): void {
  activeCascades--;
  const next = cascadeWaiters.shift();
  if (next) next();
}

// ── Helpers ──────────────────────────────────────────────────────────────────

const isComplete = (c: EnglishCourseFields & Record<string, any>) =>
  !!(c.ieltsOverall && c.pteOverall && c.toeflOverall && c.cambridgeOverall);

const englishSnapshot = (c: EnglishCourseFields & Record<string, any>) =>
  `I=${c.ieltsOverall ?? "-"} P=${c.pteOverall ?? "-"} T=${c.toeflOverall ?? "-"} C=${c.cambridgeOverall ?? "-"}`;

function visibleText(html: string): string {
  return cheerio.load(html)("body").text().replace(/\s+/g, " ").trim();
}

const VISION_FIELDS = [
  "ieltsOverall", "ieltsListening", "ieltsSpeaking", "ieltsWriting", "ieltsReading",
  "pteOverall", "pteListening", "pteSpeaking", "pteWriting", "pteReading",
  "toeflOverall", "toeflListening", "toeflSpeaking", "toeflWriting", "toeflReading",
  "cambridgeOverall", "duolingoOverall",
] as const;

function applyVisionFields(course: Record<string, any>, v: Record<string, any>): boolean {
  let any = false;
  for (const k of VISION_FIELDS) {
    const val = v?.[k];
    if (val == null) continue;
    const num = typeof val === "number" ? val : parseFloat(String(val));
    if (!Number.isFinite(num)) continue;
    if (course[k] == null) {
      course[k] = num;
      any = true;
    }
  }
  return any;
}

async function visionExtract(
  imageBuffer: Buffer,
  mimeType: string,
  context: string,
): Promise<Record<string, number> | null> {
  if (!GEMINI_API_KEY) return null;
  const base64 = imageBuffer.toString("base64");
  const prompt =
    `You are reading a university course page image. ${context}\n` +
    `Find ALL minimum English test score requirements visible in the image. ` +
    `Common: IELTS 6.0–7.5, PTE 50–65, TOEFL iBT 60–100, Cambridge CAE 169–185, Duolingo 95–130. ` +
    `Return ONLY valid JSON; omit fields you cannot see:\n` +
    `{"ieltsOverall":<n>,"ieltsListening":<n>,"ieltsSpeaking":<n>,"ieltsWriting":<n>,"ieltsReading":<n>,` +
    `"pteOverall":<n>,"pteListening":<n>,"pteSpeaking":<n>,"pteWriting":<n>,"pteReading":<n>,` +
    `"toeflOverall":<n>,"toeflListening":<n>,"toeflSpeaking":<n>,"toeflWriting":<n>,"toeflReading":<n>,` +
    `"cambridgeOverall":<n>,"duolingoOverall":<n>}`;
  const body = {
    contents: [{ parts: [{ text: prompt }, { inline_data: { mime_type: mimeType, data: base64 } }] }],
    generationConfig: { responseMimeType: "application/json", maxOutputTokens: 1024 },
  };
  try {
    const j = await callGeminiWithModelFallback(body);
    const text = j?.candidates?.[0]?.content?.parts?.[0]?.text;
    if (text) {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object") return parsed;
    }
  } catch { /* fall through */ }
  return null;
}

async function fetchAndAnalyzeImage(imageUrl: string, context: string) {
  try {
    const resp = await fetch(imageUrl, { signal: AbortSignal.timeout(20_000) });
    if (!resp.ok) return null;
    const buf = Buffer.from(await resp.arrayBuffer());
    const mime = resp.headers.get("content-type") || "image/png";
    return await visionExtract(buf, mime, context);
  } catch {
    return null;
  }
}

// ── Main entry point ─────────────────────────────────────────────────────────

export async function extractEnglishWithCascade(
  url: string,
  course: EnglishCourseFields & Record<string, any>,
  opts: { courseName: string; degreeLevel?: string; timeoutMs?: number } = { courseName: "" },
): Promise<CascadeOutcome> {
  const steps: string[] = [];
  const evidence: CascadeEvidence[] = [];

  if (isComplete(course)) {
    steps.push("skip:already-complete");
    return { steps, evidence };
  }

  let playwright: typeof import("playwright") | null = null;
  try {
    playwright = await import("playwright");
  } catch {
    steps.push("fail:no-playwright");
    return { steps, evidence };
  }

  let executablePath: string | undefined;
  try {
    const { execSync } = await import("child_process");
    const found = execSync(
      "which chromium 2>/dev/null || which chromium-browser 2>/dev/null || true",
    )
      .toString()
      .trim();
    if (found) executablePath = found;
  } catch { /* use Playwright default */ }

  const timeoutMs = opts.timeoutMs ?? 45_000;
  const parseCtx = {
    courseName: opts.courseName,
    degreeLevel: opts.degreeLevel,
    allowCefrFloor: true,
  };

  let browser: import("playwright").Browser | null = null;
  await acquireCascadeSlot();
  try {
    browser = await playwright.chromium.launch({
      headless: true,
      executablePath,
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    });
    const ctx = await browser.newContext({
      userAgent:
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      viewport: { width: 1366, height: 900 },
    });
    const page = await ctx.newPage();
    try {
      await page.goto(url, { waitUntil: "networkidle", timeout: timeoutMs });
    } catch {
      try {
        await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeoutMs });
      } catch {
        steps.push("fail:goto");
        return { steps, evidence };
      }
    }
    await page.waitForTimeout(800);

    const scanCurrent = async (label: string, srcUrl: string) => {
      try {
        const html = await page.content();
        const text = visibleText(html);
        if (!hasEnglishTestKeyword(text)) return false;
        const before = englishSnapshot(course);
        const r = parseEnglishRequirementsFromText(text, "browser", parseCtx);
        applyEnglishResultToCourse(course, r);
        const after = englishSnapshot(course);
        if (before !== after) {
          evidence.push({
            url: srcUrl,
            pageType: "course_page",
            extractionMethod: "cascade",
            content: `${label} → ${after}`,
          });
          steps.push(`hit:${label}`);
          return true;
        }
      } catch { /* swallow */ }
      return false;
    };

    // ── Step 1: text scan ────────────────────────────────────────────────────
    await scanCurrent("text", url);
    if (isComplete(course)) {
      steps.push("done:text");
      return { steps, evidence };
    }

    // ── Step 2: tab cascade ──────────────────────────────────────────────────
    for (const pattern of TAB_PATTERNS) {
      if (isComplete(course)) break;
      try {
        const locator = page.locator(
          `a:has-text("${pattern}"), button:has-text("${pattern}"), [role="tab"]:has-text("${pattern}"), li:has-text("${pattern}") > a, li:has-text("${pattern}") > button`,
        );
        const total = await locator.count().catch(() => 0);
        const limit = Math.min(total, 4);
        for (let i = 0; i < limit; i++) {
          if (isComplete(course)) break;
          try {
            await locator.nth(i).click({ timeout: 3000, force: true });
            await page.waitForTimeout(500);
            await scanCurrent(`tab:${pattern.slice(0, 24)}`, url);
          } catch { /* try next */ }
        }
      } catch { /* pattern skipped */ }
    }
    if (isComplete(course)) {
      steps.push("done:tabs");
      return { steps, evidence };
    }

    // ── Step 3: modal cascade ────────────────────────────────────────────────
    for (const pattern of MODAL_PATTERNS) {
      if (isComplete(course)) break;
      try {
        const locator = page.locator(
          `a:has-text("${pattern}"), button:has-text("${pattern}"), [role="button"]:has-text("${pattern}")`,
        );
        const total = await locator.count().catch(() => 0);
        const limit = Math.min(total, 4);
        for (let i = 0; i < limit; i++) {
          if (isComplete(course)) break;
          try {
            await locator.nth(i).click({ timeout: 3000, force: true });
            await page.waitForTimeout(500);
            await scanCurrent(`modal:${pattern.slice(0, 24)}`, url);
          } catch { /* try next */ }
        }
      } catch { /* pattern skipped */ }
    }
    if (isComplete(course)) {
      steps.push("done:modals");
      return { steps, evidence };
    }

    // ── Step 4: vision-AI on candidate images, then full-page screenshot ────
    if (GEMINI_API_KEY) {
      // 4a — candidate <img>s on the page
      try {
        const html = await page.content();
        const $ = cheerio.load(html);
        const seen = new Set<string>();
        const candidates: string[] = [];
        $("img").each((_, el) => {
          const src = $(el).attr("src") || $(el).attr("data-src") || "";
          if (!src) return;
          let abs: string;
          try {
            abs = new URL(src, url).href;
          } catch {
            return;
          }
          if (seen.has(abs)) return;
          const alt =
            ($(el).attr("alt") || "") +
            " " +
            ($(el).attr("title") || "") +
            " " +
            ($(el).parent().text() || "");
          const haystack = (abs + " " + alt).toLowerCase();
          // Prioritise images that smell like requirement tables; fall back to
          // any reasonably-sized raster image.
          if (
            /english|entry|admission|requirement|ielts|pte|toefl|cambridge|score|table|chart/.test(
              haystack,
            ) ||
            /\.(png|jpe?g|webp)(\?|$)/i.test(abs)
          ) {
            seen.add(abs);
            candidates.push(abs);
          }
        });

        for (const imgUrl of candidates.slice(0, 6)) {
          if (isComplete(course)) break;
          const v = await fetchAndAnalyzeImage(
            imgUrl,
            `English requirements image for: ${opts.courseName}`,
          );
          if (v && applyVisionFields(course, v)) {
            evidence.push({
              url: imgUrl,
              pageType: "course_page",
              extractionMethod: "ai",
              content: `vision-img → ${englishSnapshot(course)} (raw: ${JSON.stringify(v).slice(0, 200)})`,
            });
            steps.push("hit:vision-img");
          }
        }
      } catch { /* swallow */ }

      // 4b — full-page screenshot fallback
      if (!isComplete(course)) {
        try {
          const buf = await page.screenshot({ fullPage: true, type: "png", timeout: 15_000 });
          const v = await visionExtract(
            Buffer.from(buf),
            "image/png",
            `Full screenshot of a university course page. Course: ${opts.courseName}.`,
          );
          if (v && applyVisionFields(course, v)) {
            evidence.push({
              url,
              pageType: "course_page",
              extractionMethod: "ai",
              content: `vision-screenshot → ${englishSnapshot(course)} (raw: ${JSON.stringify(v).slice(0, 200)})`,
            });
            steps.push("hit:vision-screenshot");
          }
        } catch { /* swallow */ }
      }
    }
    if (isComplete(course)) {
      steps.push("done:vision");
      return { steps, evidence };
    }

    // ── Step 5: follow in-page links (English / admission / requirement) ────
    try {
      const html = await page.content();
      const $ = cheerio.load(html);
      const linkSet = new Set<string>();
      $("a[href]").each((_, el) => {
        const href = $(el).attr("href") || "";
        const txt = $(el).text();
        if (!href) return;
        let abs: string;
        try {
          abs = new URL(href, url).href;
        } catch {
          return;
        }
        if (abs === url || abs.startsWith("javascript:") || abs.startsWith("mailto:")) return;
        const haystack = (href + " " + txt).toLowerCase();
        if (
          /english|admission|entry|requirement|language|ielts|pte|toefl/.test(haystack)
        ) {
          linkSet.add(abs);
        }
      });

      for (const linkUrl of Array.from(linkSet).slice(0, 4)) {
        if (isComplete(course)) break;
        let sub: import("playwright").Page | null = null;
        try {
          sub = await ctx.newPage();
          await sub.goto(linkUrl, { waitUntil: "domcontentloaded", timeout: 20_000 });
          await sub.waitForTimeout(400);
          const subHtml = await sub.content();
          const subText = visibleText(subHtml);
          if (hasEnglishTestKeyword(subText)) {
            const before = englishSnapshot(course);
            applyEnglishResultToCourse(
              course,
              parseEnglishRequirementsFromText(subText, "browser", parseCtx),
            );
            const after = englishSnapshot(course);
            if (before !== after) {
              evidence.push({
                url: linkUrl,
                pageType: "english_page",
                extractionMethod: "cascade",
                content: `link-follow → ${after}`,
              });
              steps.push("hit:link");
            }
          }
        } catch { /* swallow */ } finally {
          if (sub) {
            try { await sub.close(); } catch { /* ignore */ }
          }
        }
      }
    } catch { /* swallow */ }

    if (isComplete(course)) steps.push("done:links");
    else steps.push("end:incomplete");
    return { steps, evidence };
  } catch (err) {
    steps.push(`fail:${(err as Error).message?.slice(0, 60) || "unknown"}`);
    return { steps, evidence };
  } finally {
    if (browser) {
      try { await browser.close(); } catch { /* ignore */ }
    }
    releaseCascadeSlot();
  }
}
