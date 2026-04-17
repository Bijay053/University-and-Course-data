import { Router, type IRouter, type Request, type Response } from "express";
import * as cheerio from "cheerio";
import { pool, db, universitiesTable, scrapedCoursesTable } from "@workspace/db";
import { eq, and } from "drizzle-orm";
import { fetchPageWithBrowser, siteNeedsBrowser } from "../browser-helper.js";

const router: IRouter = Router();

const GEMINI_API_KEY = process.env.GEMINI_API_KEY;
const GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash-001", "gemini-2.0-flash-lite-001"];
function geminiUrl(model: string) {
  return `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${GEMINI_API_KEY}`;
}

interface CourseData {
  courseName: string;
  category?: string;
  subCategory?: string;
  courseWebsite?: string;
  duration?: number;
  durationTerm?: string;
  studyMode?: string;
  degreeLevel?: string;
  studyLoad?: string;
  language?: string;
  description?: string;
  intakeMonths?: string[];
  intakeDays?: number;
  internationalFee?: number;
  feeTerm?: string;
  feeYear?: number;
  currency?: string;
  ieltsOverall?: number;
  ieltsListening?: number;
  ieltsSpeaking?: number;
  ieltsWriting?: number;
  ieltsReading?: number;
  pteOverall?: number;
  pteListening?: number;
  pteSpeaking?: number;
  pteWriting?: number;
  pteReading?: number;
  toeflOverall?: number;
  toeflListening?: number;
  toeflSpeaking?: number;
  toeflWriting?: number;
  toeflReading?: number;
  cambridgeOverall?: number;
  duolingoOverall?: number;
  academicLevel?: string;
  academicScore?: number;
  scoreType?: string;
  academicCountry?: string;
  otherRequirement?: string;
  scholarship?: string;
}

interface ScrapeConfig {
  courseLinks: { url: string; name: string }[];
  uniPages: { feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string };
  resolvedUrl: string;
  lastScrapedAt: string;
}

interface ApprovalSummary {
  totalCourses: number;
  validSamples: number;
  rejectedSamples: number;
  sampleTotal: number;
  validExamples: string[];
  rejectedExamples: string[];
  estimatedMinutes: number;
}

interface ScrapeJob {
  id: string;
  status: "running" | "completed" | "failed" | "stopped" | "awaiting_approval";
  logs: { event: string; [key: string]: unknown }[];
  imported: number;
  skipped: number;
  errors: number;
  totalFound: number;
  current: number;
  startedAt: number;
  completedAt?: number;
  universityId?: number;
  universityName?: string;
  url?: string;
  stopped?: boolean;
  fastMode?: boolean;
  discoveredConfig?: ScrapeConfig;
  approvalSummary?: ApprovalSummary;
  awaitingApproval?: { resolve: (proceed: boolean) => void; summary: ApprovalSummary };
}

const scrapeJobs = new Map<string, ScrapeJob>();

function addLog(job: ScrapeJob, event: string, data: Record<string, unknown> = {}) {
  job.logs.push({ event, ...data });
  if (job.logs.length > 2000) job.logs = job.logs.slice(-1500);
}

function waitForApproval(job: ScrapeJob, summary: ApprovalSummary): Promise<boolean> {
  return new Promise((resolve) => {
    job.awaitingApproval = { resolve, summary };
    job.status = "awaiting_approval";
    addLog(job, "approval_required", {
      ...summary,
      message: `Research complete. Found ${summary.totalCourses} course pages to fetch. Please review and confirm.`,
      phase: "awaiting_approval",
    });
  });
}

async function geminiChat(systemPrompt: string, userContent: string, maxTokens = 8192): Promise<string> {
  if (!GEMINI_API_KEY) throw new Error("GEMINI_API_KEY not configured");

  const body = JSON.stringify({
    system_instruction: { parts: [{ text: systemPrompt }] },
    contents: [{ parts: [{ text: userContent }] }],
    generationConfig: {
      responseMimeType: "application/json",
      maxOutputTokens: maxTokens,
    },
  });

  for (const model of GEMINI_MODELS) {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const resp = await fetch(geminiUrl(model), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
        });

        if (resp.status === 429 || resp.status === 503) {
          console.log(`Gemini ${model} returned ${resp.status}, ${attempt === 0 ? "retrying..." : "trying next model..."}`);
          if (attempt === 0) { await new Promise((r) => setTimeout(r, 5000)); continue; }
          break;
        }
        if (resp.status === 404) { console.log(`Gemini model ${model} not available, trying next...`); break; }
        if (!resp.ok) {
          const errText = await resp.text();
          throw new Error(`Gemini API error ${resp.status}: ${errText.slice(0, 300)}`);
        }

        const data = await resp.json() as any;
        const text = data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
        if (!text) { console.log(`Empty response from ${model}, trying next...`); break; }
        console.log(`Gemini response OK from ${model}`);
        return text;
      } catch (err) {
        if ((err as Error).message.includes("Gemini API error")) throw err;
        console.log(`Gemini ${model} attempt ${attempt + 1} failed: ${(err as Error).message}`);
        if (attempt === 0) await new Promise((r) => setTimeout(r, 3000));
      }
    }
  }
  throw new Error("All Gemini models are currently unavailable. Please try again in a minute.");
}

// ── Stealth browser profiles (rotate on 403 to bypass WAF fingerprinting) ────
const STEALTH_PROFILES = [
  {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-platform": '"Windows"',
  },
  {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-platform": '"macOS"',
  },
  {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "sec-ch-ua": '"Firefox";v="125"',
    "sec-ch-ua-platform": '"Windows"',
  },
];
const STEALTH_COMMON_HEADERS: Record<string, string> = {
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
  "Accept-Language": "en-US,en;q=0.9",
  "Accept-Encoding": "gzip, deflate, br",
  "Referer": "https://www.google.com/",
  "Sec-Fetch-Dest": "document",
  "Sec-Fetch-Mode": "navigate",
  "Sec-Fetch-Site": "cross-site",
  "Sec-Fetch-User": "?1",
  "Upgrade-Insecure-Requests": "1",
  "Cache-Control": "max-age=0",
  "sec-ch-ua-mobile": "?0",
};

async function fetchPage(url: string): Promise<string> {
  let lastStatus = 0;
  // Try each stealth profile in turn
  for (let i = 0; i < STEALTH_PROFILES.length; i++) {
    try {
      const resp = await fetch(url, {
        headers: { ...STEALTH_PROFILES[i], ...STEALTH_COMMON_HEADERS },
        signal: AbortSignal.timeout(18000),
      });
      if (resp.ok) return await resp.text();
      lastStatus = resp.status;
      // Only retry on 403/429; fail fast on 404, 5xx etc.
      if (resp.status !== 403 && resp.status !== 429) throw new Error(`HTTP ${resp.status} for ${url}`);
      if (i < STEALTH_PROFILES.length - 1) await new Promise(r => setTimeout(r, 800 * (i + 1)));
    } catch (err) {
      const msg = (err as Error).message;
      if (msg.startsWith("HTTP ") && !msg.includes("403") && !msg.includes("429")) throw err;
      if (i === STEALTH_PROFILES.length - 1 && lastStatus !== 403 && lastStatus !== 429) throw err;
    }
  }
  // Stealth profiles exhausted — try headless browser
  try {
    const browserResult = await fetchPageWithBrowser(url, {});
    if (browserResult?.mainHtml) return browserResult.mainHtml;
  } catch {}
  // Last resort: Google cache
  try {
    const cacheUrl = `https://webcache.googleusercontent.com/search?q=cache:${encodeURIComponent(url)}`;
    const resp = await fetch(cacheUrl, {
      headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS },
      signal: AbortSignal.timeout(12000),
    });
    if (resp.ok) {
      const html = await resp.text();
      if (html.length > 1000) return html;
    }
  } catch {}
  throw new Error(`HTTP 403 for ${url} (all fallbacks failed)`);
}

function extractCompactContent(html: string, url: string): string {
  const $ = cheerio.load(html);
  $("script, style, noscript, iframe, svg, .cookie, .chat, .popup").remove();
  $(".hidden:not(.w-tab-pane):not([class*='tab']), [aria-hidden='true']:not([class*='tab'])").remove();

  const sections: string[] = [];
  const mainContent = $("main, [role='main'], .content, .course-detail, .course-info, article, .w-tab-content, .tab-content").first();
  const target = mainContent.length ? mainContent : $("body");

  target.find("h1, h2, h3, h4").each((_, el) => {
    const heading = $(el).text().trim();
    const next = $(el).nextUntil("h1, h2, h3, h4").text().replace(/\s+/g, " ").trim();
    if (heading && (heading.length + next.length) > 10) {
      sections.push(`## ${heading}\n${next.slice(0, 500)}`);
    }
  });

  target.find("table").each((_, el) => {
    const rows: string[] = [];
    $(el).find("tr").each((_, row) => {
      const cells: string[] = [];
      $(row).find("th, td").each((_, cell) => cells.push($(cell).text().trim()));
      if (cells.length > 0) rows.push(cells.join(" | "));
    });
    if (rows.length > 0) sections.push(rows.join("\n"));
  });

  target.find("dl").each((_, el) => {
    $(el).find("dt").each((_, dt) => {
      const label = $(dt).text().trim();
      const value = $(dt).next("dd").text().trim();
      if (label && value) sections.push(`${label}: ${value}`);
    });
  });

  let result = sections.join("\n\n");
  if (result.length < 200) {
    result = target.text().replace(/\s+/g, " ").trim().slice(0, 8000);
  }

  const imgNotes: string[] = [];
  $("img[src]").each((_, el) => {
    const src = $(el).attr("src") || "";
    if (/fee|ielts|english|requirement|tuition/i.test(src)) {
      imgNotes.push(`[IMAGE: ${src}]`);
    }
  });

  const pdfNotes: string[] = [];
  $("a[href*='.pdf']").each((_, el) => {
    const href = $(el).attr("href") || "";
    const text = $(el).text().trim();
    if (/fee|tuition|international|price/i.test(href + " " + text)) {
      pdfNotes.push(`[PDF LINK: ${text} -> ${href}]`);
    }
  });

  const extra = [...imgNotes, ...pdfNotes].join("\n");

  return `URL: ${url}\n\n${result.slice(0, 8000)}${extra ? "\n\nNOTES:\n" + extra : ""}`;
}

function extractFullPageContent(html: string, url: string): string {
  const $ = cheerio.load(html);
  $("script, style, noscript, iframe, svg, nav, footer, header").remove();
  $("[style*='display:none'], [style*='display: none'], .hidden").remove();

  const baseUrl = new URL(url);
  const links: string[] = [];
  $("a[href]").each((_, el) => {
    const href = $(el).attr("href");
    const text = $(el).text().trim();
    if (href && text && text.length > 3 && text.length < 200) {
      try {
        const fullUrl = new URL(href, baseUrl.origin).toString();
        if (fullUrl.startsWith("http")) links.push(`[${text}](${fullUrl})`);
      } catch {}
    }
  });

  const bodyText = $("body").text().replace(/\s+/g, " ").trim();
  return `URL: ${url}\n\nPAGE TEXT:\n${bodyText.slice(0, 12000)}\n\nLINKS ON PAGE:\n${links.slice(0, 150).join("\n")}`;
}

function findRelatedPages(html: string, courseUrl: string): { fees?: string; requirements?: string; entry?: string; feesPdf?: string } {
  const $ = cheerio.load(html);
  const origin = new URL(courseUrl).origin;
  const result: { fees?: string; requirements?: string; entry?: string; feesPdf?: string } = {};

  $("a[href]").each((_, el) => {
    const href = $(el).attr("href") || "";
    const text = $(el).text().trim().toLowerCase();
    try {
      const fullUrl = href.startsWith("http") ? href : new URL(href, origin).toString();
      if (!fullUrl.startsWith("http")) return;

      if (!result.feesPdf && /\.pdf/i.test(fullUrl) && /fee|tuition|international/i.test(fullUrl + " " + text)) {
        result.feesPdf = fullUrl;
      }

      if (!result.fees && (
        /\b(international|overseas)\s*(fee|tuition|cost)/i.test(text) ||
        (/\b(fee|tuition|cost|pricing)/i.test(text) && !/domestic/i.test(text))
      )) {
        result.fees = fullUrl;
      }
      if (!result.requirements && /\b(entry|admission|requirement|eligib|how\s*to\s*apply)/i.test(text)) {
        result.requirements = fullUrl;
      }
      if (!result.entry && /\b(english|language|ielts|pte|toefl)/i.test(text)) {
        result.entry = fullUrl;
      }
    } catch {}
  });

  return result;
}

function findImageUrls(html: string, courseUrl: string): string[] {
  const $ = cheerio.load(html);
  const origin = new URL(courseUrl).origin;
  const images: string[] = [];

  $("img[src]").each((_, el) => {
    const src = $(el).attr("src") || "";
    const alt = ($(el).attr("alt") || "").toLowerCase();
    try {
      const fullUrl = src.startsWith("http") ? src : new URL(src, origin).toString();
      if (/fee|ielts|english|requirement|tuition|pte|toefl/i.test(fullUrl + " " + alt)) {
        images.push(fullUrl);
      }
    } catch {}
  });

  return images;
}

/**
 * DOM-aware study mode detection.
 * Tracks hasOnline and hasOnCampus independently, combining them to "Blended".
 * Handles "Location: Sydney, Online" + "Delivery: Face to Face" → Blended.
 */
function detectStudyMode($: ReturnType<typeof cheerio.load>, fullText: string): string {
  // ── PRIORITY 0: Title signal ──────────────────────────────────────────────
  // Some courses put the mode right in the title (e.g. UEL "Ba Hons Special
  // Education Online", "Bsc Hons Psychology Distance Learning").
  const title = (($("title").text() || "") + " " + ($("h1").first().text() || "")).toLowerCase();
  if (/\bdistance\s+learning\b/.test(title)) return "Online";
  if (/\(\s*online\s*\)|\bonline\s*$|\bonline\s+(?:study|programme?|course|degree)\b|\b(?:fully\s+)?online\s+(?:bachelor|master|diploma|certificate|mba|phd)/.test(title)) return "Online";

  // ── PRIORITY: Find an explicit "Delivery" / "Study Mode" field. ──────────
  // The "Delivery" field is authoritative — it overrides "Location" (which can
  // contain "Online" meaning an online study option, e.g. ASA's "Sydney, Online").
  // We look for label-value pairs in dt/dd, th/td, and <strong>Label</strong>+text patterns.
  const DELIVERY_LABEL = /^(?:mode\s+of\s+(?:study|delivery|attendance)|study\s*mode|delivery(?:\s*mode)?|attendance\s*mode|course\s*mode|teaching\s*mode|learning\s*mode)\s*:?\s*$/i;

  const evaluateDeliveryValue = (raw: string): string | null => {
    const v = raw.toLowerCase();
    const isOnCampus = /\b(?:face[- ]?to[- ]?face|on[- ]?campus|in[- ]?person|in\s+class(?:room)?)\b/.test(v);
    const isOnline = /\b(?:online|distance|remote|virtual)\b/.test(v);
    if (isOnCampus && isOnline) return "Blended";
    if (isOnCampus) return "On Campus";
    if (isOnline) return "Online";
    return null;
  };

  // Strategy A: <dt>Delivery</dt><dd>Face to Face</dd>
  let deliveryResult: string | null = null;
  $("dl dt").each((_, dt) => {
    if (DELIVERY_LABEL.test($(dt).text().trim())) {
      const dd = $(dt).next("dd").text().trim();
      const r = evaluateDeliveryValue(dd);
      if (r) { deliveryResult = r; return false; }
    }
  });

  // Strategy B: <tr><th>Delivery</th><td>Face to Face</td></tr>
  if (!deliveryResult) {
    $("tr").each((_, tr) => {
      const cells = $(tr).find("th,td");
      if (cells.length < 2) return;
      const label = $(cells.get(0)!).text().trim();
      if (DELIVERY_LABEL.test(label)) {
        const r = evaluateDeliveryValue($(cells.get(1)!).text().trim());
        if (r) { deliveryResult = r; return false; }
      }
    });
  }

  // Strategy C: <strong>Delivery</strong> Face to Face on campus  (label inline with value)
  if (!deliveryResult) {
    $("strong, b, h3, h4, h5, h6, span").each((_, el) => {
      const txt = $(el).text().trim();
      if (!DELIVERY_LABEL.test(txt)) return;
      // Try next sibling text first
      const sibling = $(el).next();
      let candidate = sibling.text().trim();
      // Fall back to remaining text in the parent (after this label)
      if (!candidate || candidate.length > 80) {
        const parentText = $(el).parent().text().trim();
        const idx = parentText.toLowerCase().indexOf(txt.toLowerCase());
        if (idx >= 0) candidate = parentText.slice(idx + txt.length).slice(0, 80).trim();
      }
      const r = evaluateDeliveryValue(candidate);
      if (r) { deliveryResult = r; return false; }
    });
  }

  if (deliveryResult) return deliveryResult;

  // ── PRIORITY 2: Sentence-level signals that explicitly describe delivery. ─
  // Be CONSERVATIVE: many UK university pages mention "blended learning",
  // "online learning resources", "online application" etc. as marketing
  // language — these are NOT statements of delivery mode.

  // Strong "Online" signals: course/programme is explicitly stated as online
  if (/\b(?:fully|entirely|100%)\s+online\b/i.test(fullText)) return "Online";
  if (/\b(?:course|programme?|degree|bachelor|master|diploma)\s+is\s+(?:delivered|taught|studied|offered)\s+(?:fully\s+)?online\b/i.test(fullText)) return "Online";
  if (/\bdistance[- ]learning\s+(?:course|degree|programme?|study|delivery|format|option|mode)\b/i.test(fullText)) return "Online";
  if (/\bdelivered\s+(?:fully\s+)?(?:online|remotely|by\s+distance\s+learning)\b/i.test(fullText)) return "Online";

  // Strong "Blended" signals: explicit mode-of-delivery statement
  if (/\b(?:study\s+)?mode\s*[:=]\s*blended\b/i.test(fullText)) return "Blended";
  if (/\b(?:course|programme?|degree)\s+is\s+delivered\s+(?:in\s+)?(?:a\s+)?(?:blended|hybrid)(?:\s+(?:format|mode|delivery|manner))?\b/i.test(fullText)) return "Blended";
  if (/\bblended\s+(?:delivery|mode|format|study)\b/i.test(fullText)) return "Blended";
  if (/\bhybrid\s+(?:delivery|mode|format|study)\b/i.test(fullText)) return "Blended";
  if (/\b(?:on[- ]?campus|face[- ]?to[- ]?face)\s+(?:and|or|\/)\s+online\s+(?:delivery|study|learning|teaching)\b/i.test(fullText)) return "Blended";

  // Strong "On Campus" signals
  if (/\bdelivered\s+(?:on[- ]?campus|in[- ]?person|face[- ]?to[- ]?face)\b/i.test(fullText)) return "On Campus";
  if (/\b(?:course|programme?)\s+is\s+(?:delivered|taught)\s+(?:on[- ]?campus|in[- ]?person|face[- ]?to[- ]?face)\b/i.test(fullText)) return "On Campus";

  // ── Default ─────────────────────────────────────────────────────────────
  // When no explicit delivery signal is present, assume "On Campus" — that's
  // the historical default for traditional universities.
  return "On Campus";
}

function extractWithCheerio(html: string, url: string, name: string, countryFallback?: string): Partial<CourseData> {
  const $ = cheerio.load(html);
  const text = $("body").text();
  const data: Partial<CourseData> = { courseName: name, courseWebsite: url, language: "English" };

  // Duration: prefer explicit "Duration:" label first, then fall back to general patterns
  const durLabelMatch = text.match(/(?:duration|course\s*length|program\s*length)[:\s]+(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)/i);
  const durYearMatch = text.match(/(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:full[- ]?time)?/i);
  const durMonthMatch = text.match(/(\d+)\s*months?\s*(?:full[- ]?time)?/i);
  const durWeekMatch = text.match(/(\d+)\s*weeks?\s*(?:full[- ]?time)?/i);
  const durTrimMatch = text.match(/(\d+)\s*trimesters?/i);
  const durSemMatch = text.match(/(\d+)\s*semesters?/i);

  if (durLabelMatch) {
    data.duration = parseFloat(durLabelMatch[1]);
    const t = durLabelMatch[2].toLowerCase();
    if (/year|yr/.test(t)) data.durationTerm = "Year";
    else if (/month/.test(t)) data.durationTerm = "Month";
    else if (/week/.test(t)) data.durationTerm = "Week";
    else if (/trimester/.test(t)) data.durationTerm = "Trimester";
    else if (/semester/.test(t)) data.durationTerm = "Semester";
  } else if (durYearMatch) { data.duration = parseFloat(durYearMatch[1]); data.durationTerm = "Year"; }
  else if (durMonthMatch) { data.duration = parseInt(durMonthMatch[1]); data.durationTerm = "Month"; }
  else if (durWeekMatch) { data.duration = parseInt(durWeekMatch[1]); data.durationTerm = "Week"; }
  else if (durTrimMatch) { data.duration = parseInt(durTrimMatch[1]); data.durationTerm = "Trimester"; }
  else if (durSemMatch) { data.duration = parseInt(durSemMatch[1]); data.durationTerm = "Semester"; }

  // VALIDATION: Reject unrealistic durations (prevents "21 Year" type errors)
  if (data.duration != null && data.durationTerm) {
    const termToYearFactor: Record<string, number> = {
      Year: 1, Month: 1 / 12, Week: 1 / 52, Trimester: 1 / 3, Semester: 1 / 2,
    };
    const factor = termToYearFactor[data.durationTerm] ?? 1;
    const durationInYears = data.duration * factor;
    if (durationInYears > 10 || durationInYears < 0.25) {
      console.log(`[WARNING] Rejected unrealistic duration: ${data.duration} ${data.durationTerm} (${durationInYears.toFixed(2)} yrs)`);
      data.duration = undefined;
      data.durationTerm = undefined;
    }
  }

  if (/full[- ]?time\s*(and|or|\/)\s*part[- ]?time/i.test(text)) data.studyLoad = "Full Time";
  else if (/full[- ]?time/i.test(text)) data.studyLoad = "Full Time";
  else if (/part[- ]?time/i.test(text)) data.studyLoad = "Part Time";

  // Study mode — DOM-aware detection, checks Location and Delivery fields independently
  data.studyMode = detectStudyMode($, text);

  const lower = name.toLowerCase();
  if (/\bphd\b|doctor of philosophy/i.test(lower)) data.degreeLevel = "PhD";
  else if (/\bmaster\b|^m[a-z]{1,3}\b/i.test(lower)) data.degreeLevel = "Master";
  else if (/\bbachelor\b|^b[a-z]{1,3}\b/i.test(lower)) data.degreeLevel = "Bachelor";
  else if (/\bgraduate\s*(cert|dip)/i.test(lower)) data.degreeLevel = "Graduate Certificate & Diploma";
  else if (/\b(certificate|diploma)\b/i.test(lower)) data.degreeLevel = "Certificate & Diploma";
  else if (/\bassociate\s*degree/i.test(lower)) data.degreeLevel = "Associate Degree";

  extractInternationalFees(text, data, countryFallback);
  if (!data.internationalFee) extractFeeFromHtmlTables($, data, countryFallback);
  if (!data.internationalFee) extractFeeFromDomToggle($, data, countryFallback);
  extractEnglishFromHtml($, data);
  extractCountryAcademicRequirements($, data);
  extractIntakeMonths(text, data);

  const desc = $("meta[name='description']").attr("content") || $("meta[property='og:description']").attr("content") || "";
  if (desc) data.description = desc.slice(0, 500);

  return data;
}

/**
 * Extract fees from HTML tables with International/Domestic columns or rows.
 * Many universities use structured tables — this handles them precisely.
 */
function extractFeeFromHtmlTables($: ReturnType<typeof cheerio.load>, data: Partial<CourseData>, countryFallback?: string) {
  const CURR_PAT = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;

  $("table").each((_, table) => {
    if (data.internationalFee) return false;
    const $table = $(table);
    const tableText = $table.text();
    if (!CURR_PAT.test(tableText)) return;

    // Strategy A: Column headers — find "International" column index, read values
    const headerRow = $table.find("tr").first();
    const headers = headerRow.find("th, td").map((_, th) => $(th).text().trim().toLowerCase()).toArray();
    const intlColIdx = headers.findIndex(h => /international|overseas/.test(h) && !/domestic/.test(h));
    if (intlColIdx >= 0) {
      $table.find("tr").slice(1).each((_, row) => {
        if (data.internationalFee) return false;
        const cells = $(row).find("td").map((_, td) => $(td).text().trim()).toArray();
        const cellText = cells[intlColIdx] || "";
        const stripped = cellText.replace(/[,\s]/g, "").replace(/[A-Z$£€]/g, "");
        const num = parseInt(stripped);
        if (num >= 5000 && num <= 200000) {
          data.internationalFee = num;
          data.currency = detectCurrencyFromContext(cellText + tableText, countryFallback);
          data.feeTerm = normalizeFeeTerm(tableText);
          if (!data.feeYear) data.feeYear = extractFeeYear(tableText);
          return false;
        }
      });
    }
    if (data.internationalFee) return false;

    // Strategy B: Row labels — find a row containing "International" and read a fee amount from it
    $table.find("tr").each((_, row) => {
      if (data.internationalFee) return false;
      const $row = $(row);
      const cells = $row.find("td, th").map((_, td) => $(td).text().trim()).toArray();
      const rowText = cells.join(" ").toLowerCase();
      if (!/international|overseas/.test(rowText)) return;
      if (/domestic|local|resident/.test(rowText.replace(/international/g, "").replace(/overseas/g, ""))) return;

      for (const cell of cells) {
        const stripped = cell.replace(/,/g, "").replace(/[A-Z$£€\s]/g, "");
        const num = parseInt(stripped);
        if (num >= 5000 && num <= 200000) {
          data.internationalFee = num;
          data.currency = detectCurrencyFromContext(cell + tableText, countryFallback);
          data.feeTerm = normalizeFeeTerm(tableText);
          if (!data.feeYear) data.feeYear = extractFeeYear(tableText);
          return false;
        }
      }
    });
  });
}

/**
 * Detect international fee from JS-toggled DOM elements.
 * Sites like VIT use a Domestic/International button toggle — both sets of data
 * are in the HTML, one is hidden. We extract the value from the "International"
 * context by looking for:
 *  - data attributes: [data-student-type="international"], [data-view="international"]
 *  - elements with class containing "international" or "intl"
 *  - elements adjacent to an "International" label/button containing a fee amount
 */
function extractFeeFromDomToggle($: ReturnType<typeof cheerio.load>, data: Partial<CourseData>, countryFallback?: string) {
  const CURR_PAT = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;
  const feeRange = (n: number) => n >= 3000 && n <= 200000;

  function parseFee(text: string): number | null {
    const m = text.replace(/,/g, "").match(/[\d]+/);
    const n = m ? parseInt(m[0]) : NaN;
    return feeRange(n) ? n : null;
  }

  // Strategy A: data attributes explicitly marking international content
  const intlDataSelectors = [
    "[data-student-type='international']",
    "[data-view='international']",
    "[data-tab='international']",
    "[data-type='international']",
    ".international-fee", ".intl-fee", ".international .fee",
    "[class*='international'][class*='fee']",
  ];
  for (const sel of intlDataSelectors) {
    try {
      $(sel).each((_, el) => {
        if (data.internationalFee) return false;
        const txt = $(el).text();
        if (!CURR_PAT.test(txt)) return;
        const fee = parseFee(txt);
        if (fee) {
          data.internationalFee = fee;
          data.currency = detectCurrencyFromContext(txt, countryFallback);
          data.feeTerm = normalizeFeeTerm(txt);
        }
      });
    } catch {}
    if (data.internationalFee) return;
  }

  // Strategy B: find "International" label/button elements, then check siblings/parent for fee
  $("button, label, span, div, td, th, li").each((_, el) => {
    if (data.internationalFee) return false;
    const txt = $(el).text().trim();
    if (!/^international(\s+students?)?$/i.test(txt)) return;

    const $parent = $(el).parent();
    const parentText = $parent.text();
    if (!CURR_PAT.test(parentText)) return;

    // Look at siblings and parent text for a fee amount
    const fee = parseFee(parentText);
    if (fee) {
      data.internationalFee = fee;
      data.currency = detectCurrencyFromContext(parentText, countryFallback);
      data.feeTerm = normalizeFeeTerm(parentText);
    }

    // Also check next sibling
    const $next = $(el).next();
    const nextText = $next.text();
    if (!data.internationalFee && CURR_PAT.test(nextText)) {
      const fee2 = parseFee(nextText);
      if (fee2) {
        data.internationalFee = fee2;
        data.currency = detectCurrencyFromContext(nextText, countryFallback);
        data.feeTerm = normalizeFeeTerm(nextText);
      }
    }
  });
}

/**
 * Extract ALL fee amounts in a reasonable range from text.
 * If multiple found, the highest is assumed to be the international fee.
 */
function extractAllFeeAmounts(text: string): number[] {
  const amounts: number[] = [];
  const CURR_TOKENS = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;
  const pattern = new RegExp(`(?:${CURR_TOKENS.source})\\s*([\\d,]+)|([\\d,]+)\\s*(?:${CURR_TOKENS.source})`, "gi");
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(text)) !== null) {
    const raw = (m[1] || m[2] || "").replace(/,/g, "");
    const num = parseInt(raw);
    if (num >= 5000 && num <= 200000 && !amounts.includes(num)) amounts.push(num);
  }
  return amounts;
}

function normalizeFeeTerm(context: string): string {
  if (/per\s*trimester|per\s*trim\b/i.test(context)) return "Trimester";
  if (/per\s*semester/i.test(context)) return "Semester";
  if (/per\s*term\b/i.test(context)) return "Term";
  if (/per\s*session\b/i.test(context)) return "Session";
  if (/per\s*(credit\s*)?unit|per\s*point|per\s*credit/i.test(context)) return "Per Unit";
  if (/total\s*(?:course|program|tuition)|full\s*course|complete\s*(?:course|program)/i.test(context)) return "Full Course";
  if (/trimester/i.test(context)) return "Trimester";
  if (/semester/i.test(context)) return "Semester";
  if (/per\s*year|per\s*annum|p\.a\.|annual|yearly/i.test(context)) return "Annual";
  return "Annual";
}

function detectFeeTerm(context: string): string { return normalizeFeeTerm(context); }

function extractFeeYear(context: string): number | undefined {
  const currentYear = new Date().getFullYear();
  const m = context.match(/\b(20\d{2})\b/g);
  if (!m) return undefined;
  for (const y of m) {
    const yr = parseInt(y);
    if (yr >= currentYear - 1 && yr <= currentYear + 3) return yr;
  }
  return undefined;
}

const COUNTRY_CURRENCY_MAP: Record<string, string> = {
  "australia": "AUD", "new zealand": "NZD", "canada": "CAD",
  "united states": "USD", "usa": "USD", "united kingdom": "GBP",
  "uk": "GBP", "england": "GBP", "singapore": "SGD",
};

function detectCurrencyFromContext(ctx: string, countryFallback?: string): string {
  if (/NZ\$|NZD/i.test(ctx)) return "NZD";
  if (/CA\$|C\$|CAD/i.test(ctx)) return "CAD";
  if (/S\$|SGD/i.test(ctx)) return "SGD";
  if (/US\$|USD/i.test(ctx)) return "USD";
  if (/£|GBP/i.test(ctx)) return "GBP";
  if (/€|EUR/i.test(ctx)) return "EUR";
  if (/A\$|AUD/i.test(ctx)) return "AUD";
  if (countryFallback) {
    const mapped = COUNTRY_CURRENCY_MAP[countryFallback.toLowerCase()];
    if (mapped) return mapped;
  }
  return "AUD";
}

function extractInternationalFees(text: string, data: Partial<CourseData>, countryFallback?: string) {
  const CURRENCY_SYM = /(?:AUD|NZD|CAD|USD|GBP|SGD|EUR|A\$|NZ\$|CA\$|US\$|S\$|£|€|\$)/;

  function applyFee(matchStr: string, feeStr: string) {
    const fee = parseInt(feeStr.replace(/,/g, ""));
    if (fee <= 1000 || fee >= 200000) return false;
    data.internationalFee = fee;
    data.currency = detectCurrencyFromContext(matchStr, countryFallback);
    data.feeTerm = normalizeFeeTerm(matchStr);
    if (!data.feeYear) data.feeYear = extractFeeYear(matchStr);
    return true;
  }

  // Priority 0 (highest): "Total fee (per-unit rate)" pattern
  // e.g. "$48,000 ($3,000/unit)" or "$36,000 ($1,500/unit)" for domestic
  // VIT shows BOTH domestic and international on the same page — take the LARGEST total
  // (international is always higher than domestic, so max = international fee)
  const perUnitTotalPat = new RegExp(
    `(?:fees?[:\\s]*)?${CURRENCY_SYM.source}\\s*([\\d,]+)\\s*\\(${CURRENCY_SYM.source}?\\s*[\\d,]+\\s*/\\s*(?:unit|credit|point|subject)\\)`,
    "gi"
  );
  const perUnitMatches = [...text.matchAll(perUnitTotalPat)];
  if (perUnitMatches.length > 0) {
    let bestTotal = 0;
    let bestMatch = perUnitMatches[0];
    for (const m of perUnitMatches) {
      const fee = parseInt(m[1].replace(/,/g, ""));
      if (fee > bestTotal && fee >= 3000 && fee <= 200000) {
        bestTotal = fee;
        bestMatch = m;
      }
    }
    if (bestTotal > 0) {
      data.internationalFee = bestTotal;
      data.currency = detectCurrencyFromContext(bestMatch[0], countryFallback);
      data.feeTerm = "Full Course";
      if (!data.feeYear) data.feeYear = extractFeeYear(text);
      return;
    }
  }

  // Priority 1: explicit international section with currency
  const intlSection = text.match(
    new RegExp(`international[^]*?(?:fee|tuition|cost)[^]*?${CURRENCY_SYM.source}\\s*([\\d,]+)`, "i")
  );
  if (intlSection && applyFee(intlSection[0], intlSection[1])) return;

  // Priority 2: explicit international/overseas/non-resident label patterns
  const feePatterns = [
    // "International student fee: $42,000"
    new RegExp(`(?:international|overseas|non-?resident)\\s*(?:student\\s*)?(?:fee|tuition|cost)[:\\s]*${CURRENCY_SYM.source}?\\s*([\\d,]+)`, "i"),
    // "International students: AUD $38,000"
    new RegExp(`(?:international|overseas|non-?resident)[^.]*?${CURRENCY_SYM.source}\\s*([\\d,]+)`, "i"),
    // HTML table: <td>International</td><td>$42,000</td>
    /<td[^>]*>\s*(?:International|Overseas)\s*<\/td>\s*<td[^>]*>\s*(?:AUD|NZD|CAD|USD|GBP|SGD|€|\$|£|A\$)?\s*([\d,]+)/i,
    // "Fee: $38,000 per year (international)"
    new RegExp(`${CURRENCY_SYM.source}\\s*([\\d,]+)[^.]*?(?:international|overseas)`, "i"),
  ];
  for (const fp of feePatterns) {
    const fm = text.match(fp);
    if (fm && applyFee(fm[0], fm[1])) return;
  }

  // Priority 3: generic fee not explicitly domestic
  const genericFee = text.match(
    new RegExp(`(?:tuition|fee|cost)[:\\s]*${CURRENCY_SYM.source}\\s*([\\d,]+)`, "i")
  );
  if (genericFee && !/domestic|resident|local/i.test(genericFee[0])) {
    const fee = parseInt(genericFee[1].replace(/,/g, ""));
    if (fee > 5000 && fee < 200000) {
      applyFee(genericFee[0], genericFee[1]);
    }
  }

  // Priority 4: Collect ALL currency amounts — if 2+ found, highest is likely international
  if (!data.internationalFee) {
    const allAmounts = extractAllFeeAmounts(text);
    if (allAmounts.length >= 2) {
      // Multiple amounts: assume higher = international (domestic is always lower)
      const maxFee = Math.max(...allAmounts);
      data.internationalFee = maxFee;
      data.currency = detectCurrencyFromContext(text, countryFallback);
      data.feeTerm = normalizeFeeTerm(text);
      if (!data.feeYear) data.feeYear = extractFeeYear(text);
    } else if (allAmounts.length === 1 && !data.internationalFee) {
      data.internationalFee = allAmounts[0];
      data.currency = detectCurrencyFromContext(text, countryFallback);
      data.feeTerm = normalizeFeeTerm(text);
      if (!data.feeYear) data.feeYear = extractFeeYear(text);
    }
  }
}

/**
 * Parse a single English test requirement cell text into overall + min band scores.
 * Used by extractEnglishFromHtml table parsing.
 */
function parseEnglishTestCell(testType: string, reqText: string, data: Partial<CourseData>) {
  const tl = testType.toLowerCase();

  if (/ielts/.test(tl)) {
    // "Overall Band Score 6.0 with a minimum sub-score of 5.5 ..."
    // "6.5 (no band less than 6.0)" / "Overall 6.0, min 5.5"
    const withMinM = reqText.match(/(?:overall|band)?\s*(?:score)?\s*([\d.]+)[^\d]*(?:minimum|no\s+(?:band|score)\s+(?:less|lower|below)\s+than|sub[-\s]?score)[^\d]*([\d.]+)/i);
    if (withMinM) {
      const overall = parseFloat(withMinM[1]);
      const min = parseFloat(withMinM[2]);
      if (overall >= 4 && overall <= 9) {
        data.ieltsOverall = overall;
        if (min >= 4 && min <= 9) {
          if (!data.ieltsListening) data.ieltsListening = min;
          if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
          if (!data.ieltsWriting) data.ieltsWriting = min;
          if (!data.ieltsReading) data.ieltsReading = min;
        }
        return;
      }
    }
    const simpleM = reqText.match(/([\d.]+)/);
    if (simpleM) {
      const v = parseFloat(simpleM[1]);
      if (v >= 4 && v <= 9 && !data.ieltsOverall) data.ieltsOverall = v;
    }

  } else if (/pte|pearson/.test(tl)) {
    const withMinM = reqText.match(/(?:overall)?\s*(?:score)?\s*(\d+)[^\d]*(?:minimum|no\s+(?:skill|score)\s+(?:less|lower|below)\s+than)[^\d]*(\d+)/i);
    if (withMinM) {
      const overall = parseInt(withMinM[1]);
      const min = parseInt(withMinM[2]);
      if (overall >= 30 && overall <= 90) {
        data.pteOverall = overall;
        if (min >= 30 && min <= 90) {
          if (!data.pteListening) data.pteListening = min;
          if (!data.pteSpeaking) data.pteSpeaking = min;
          if (!data.pteWriting) data.pteWriting = min;
          if (!data.pteReading) data.pteReading = min;
        }
        return;
      }
    }
    const simpleM = reqText.match(/(\d+)/);
    if (simpleM) {
      const v = parseInt(simpleM[1]);
      if (v >= 30 && v <= 90 && !data.pteOverall) data.pteOverall = v;
    }

  } else if (/toefl/.test(tl)) {
    const withMinM = reqText.match(/(\d+)[^\d]*(?:minimum|no\s+(?:section|score)\s+(?:less|lower|below)\s+than)[^\d]*(\d+)/i);
    if (withMinM) {
      const overall = parseInt(withMinM[1]);
      const min = parseInt(withMinM[2]);
      if (overall >= 30 && overall <= 120) {
        data.toeflOverall = overall;
        if (min >= 0 && min <= 30) {
          if (!data.toeflListening) data.toeflListening = min;
          if (!data.toeflSpeaking) data.toeflSpeaking = min;
          if (!data.toeflWriting) data.toeflWriting = min;
          if (!data.toeflReading) data.toeflReading = min;
        }
        return;
      }
    }
    const simpleM = reqText.match(/(\d+)/);
    if (simpleM) {
      const v = parseInt(simpleM[1]);
      if (v >= 30 && v <= 120 && !data.toeflOverall) data.toeflOverall = v;
    }

  } else if (/cae|cambridge/.test(tl)) {
    const m = reqText.match(/(\d+)/);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 140 && v <= 230 && !data.cambridgeOverall) data.cambridgeOverall = v;
    }

  } else if (/duolingo|det/.test(tl)) {
    const m = reqText.match(/(\d+)/);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 50 && v <= 160 && !data.duolingoOverall) data.duolingoOverall = v;
    }
  }
}

// ── Country-based academic requirement table parser ────────────────────────────
// Finds tables with a "Country" header and extracts per-country qualification
// requirements (Nepal GPA, Bangladesh HSC, India %, etc.).
// Primary row → academicCountry / academicLevel / academicScore / scoreType.
// All rows (when >1) → stored as pipe-separated summary in otherRequirement.
const KNOWN_COUNTRIES = new Set([
  "nepal","bangladesh","india","pakistan","sri lanka","nigeria","china","indonesia",
  "philippines","vietnam","kenya","ghana","zimbabwe","cameroon","malaysia","thailand",
  "myanmar","cambodia","laos","mauritius","tanzania","uganda","ethiopia","zambia",
  "south korea","hong kong","taiwan","saudi arabia","uae","qatar","oman","jordan",
  "egypt","iran","iraq","morocco","algeria","germany","france","italy","russia",
  "ukraine","poland","turkey","brazil","colombia","peru","mexico","argentina",
  "south africa","botswana","namibia","malawi","rwanda","senegal","côte d'ivoire",
  "cote d'ivoire","ivory coast","eritrea","somalia","sudan","south sudan",
  "new zealand","australia","usa","canada","united states","united kingdom","uk",
]);

function extractCountryAcademicRequirements(
  $: ReturnType<typeof cheerio.load>,
  data: Partial<CourseData>
): void {
  if (data.academicCountry && data.academicScore) return;

  const countryRows: { country: string; level: string; score?: number; scoreType?: string }[] = [];

  $("table").each((_, table) => {
    const $table = $(table);
    const rawHeaders = $table.find("thead tr th, tr:first-child th, tr:first-child td")
      .map((_, el) => $(el).text().trim().toLowerCase()).get();

    const countryColIdx = rawHeaders.findIndex(h => /\bcountry\b|\bnation\b/.test(h));
    if (countryColIdx === -1) return;

    const qualColIdx = rawHeaders.findIndex(h => /qualif|level|education|study|school|subject/.test(h));
    const gradeColIdx = rawHeaders.findIndex(h => /grade|gpa|score|requirement|mark|result|point/.test(h));

    $table.find("tbody tr, tr").each((rowIdx, row) => {
      if (rowIdx === 0 && countryColIdx < rawHeaders.length) return; // header row
      const cells = $(row).find("td").map((_, td) => $(td).text().trim().replace(/\s+/g, " ")).get();
      if (cells.length < 2) return;

      const rawCountry = (cells[countryColIdx] || "").toLowerCase();
      if (!rawCountry) return;

      const isKnown = KNOWN_COUNTRIES.has(rawCountry) ||
        Array.from(KNOWN_COUNTRIES).some(c => rawCountry.startsWith(c) || rawCountry.includes(c));
      if (!isKnown) return;

      const country = cells[countryColIdx] || "";
      const level = qualColIdx >= 0 ? (cells[qualColIdx] || "") : "";
      const gradeText = gradeColIdx >= 0 ? (cells[gradeColIdx] || "") : (cells[cells.length - 1] || "");

      let score: number | undefined;
      let scoreType: string | undefined;

      const gpaM = gradeText.match(/gpa\s*(?:of\s*|:)?\s*(\d+(?:\.\d+)?)/i);
      const percM = gradeText.match(/(\d+(?:\.\d+)?)\s*%/);
      const outOfM = gradeText.match(/(\d+(?:\.\d+)?)\s*out\s*of\s*(\d+(?:\.\d+)?)/i);
      const cgpaM = gradeText.match(/cgpa\s*(?:of\s*|:)?\s*(\d+(?:\.\d+)?)/i);

      if (cgpaM) { score = parseFloat(cgpaM[1]); scoreType = "CGPA"; }
      else if (gpaM) { score = parseFloat(gpaM[1]); scoreType = "GPA"; }
      else if (percM) { score = parseFloat(percM[1]); scoreType = "Percentage"; }
      else if (outOfM) { score = parseFloat(outOfM[1]); scoreType = `Score (/${outOfM[2]})`; }

      if (!score) {
        const numM = gradeText.match(/(\d+(?:\.\d+)?)/);
        if (numM) { score = parseFloat(numM[1]); scoreType = "Score"; }
      }

      countryRows.push({ country, level, score, scoreType });
    });
  });

  if (countryRows.length === 0) return;

  const primary = countryRows[0];
  if (!data.academicCountry) data.academicCountry = primary.country;
  if (!data.academicLevel && primary.level) data.academicLevel = primary.level;
  if (primary.score && !data.academicScore) data.academicScore = primary.score;
  if (primary.scoreType && !data.scoreType) data.scoreType = primary.scoreType;

  if (countryRows.length > 1 && !data.otherRequirement) {
    const summary = countryRows
      .map(r => `${r.country}: ${r.level}${r.score ? ` (${r.scoreType ?? "Score"} ${r.score})` : ""}`)
      .join(" | ");
    data.otherRequirement = summary;
  }
}

/**
 * Tab/Section-aware English test extraction.
 * Strategy:
 *   1. Find "Entry Requirements" section by ID, class, or heading → try table → try text
 *   2. Fall back to full-page text extraction
 */
function extractEnglishFromHtml($: ReturnType<typeof cheerio.load>, data: Partial<CourseData>) {
  // ── Strategy 0: Run high-priority body text scan first (catches VIT format) ──
  // Pattern -1 inside extractEnglishRequirements handles "IELTS Academic: Overall score 6.5,
  // with no band below 6.0" — common across many AU universities. Running this first
  // ensures it isn't blocked by an earlier section-based pattern that finds nothing useful.
  if (!data.ieltsOverall) {
    const bodyText = $("body").text();
    extractEnglishRequirements(bodyText, data);
    if (data.ieltsOverall && data.pteOverall && data.toeflOverall) return;
  }

  // ── Strategy 1: Find entry requirements section ──────────────────────────
  const reqSelectors = [
    "[id*='entry'i][id*='requirement'i]",
    "[id*='admission'i][id*='requirement'i]",
    "[id*='requirement'i]",
    "[id*='english'i][id*='requirement'i]",
    "[id*='english'i][id*='proficiency'i]",
    "[class*='entry'i][class*='requirement'i]",
    "[class*='admission'i][class*='requirement'i]",
    "[class*='requirement'i]",
    "[class*='english'i][class*='proficiency'i]",
    "[role='tabpanel']",
    "section, article, div",
  ];

  let reqContainer: ReturnType<typeof $> | null = null;

  // Try attribute-based selectors first (all except the two generic fallbacks at the end)
  for (const sel of reqSelectors.slice(0, 9)) {
    const el = $(sel).first();
    if (el.length) { reqContainer = el; break; }
  }

  // Fallback: find by heading text
  if (!reqContainer) {
    $("h1,h2,h3,h4,h5").each((_, heading) => {
      const headingText = $(heading).text();
      if (/entry\s+requirements?|admission\s+requirements?|english\s+(?:language\s+)?requirements?|language\s+requirements?|english\s+proficiency/i.test(headingText)) {
        const parent = $(heading).closest("div,section,article");
        if (parent.length) { reqContainer = parent; return false; }
      }
    });
  }

  // Fallback: find tabpanel/section containing IELTS text
  if (!reqContainer) {
    $("[role='tabpanel'], section, .tab-content, .accordion-content").each((_, el) => {
      if (/ielts|english\s+(?:language|proficiency|test)/i.test($(el).text())) {
        reqContainer = $(el);
        return false;
      }
    });
  }

  if (reqContainer) {
    // ── Strategy 1a: Table parsing inside the section ────────────────────
    let foundInTable = false;
    reqContainer.find("table").each((_, table) => {
      const $table = $(table);
      $table.find("tr").each((_, row) => {
        const cells = $(row).find("td,th");
        if (cells.length < 2) return;
        const testType = $(cells.get(0)!).text().trim();
        const reqText = $(cells.get(1)!).text().trim();
        if (/ielts|pte|toefl|cae|cambridge|duolingo|det|pearson/i.test(testType)) {
          parseEnglishTestCell(testType, reqText, data);
          foundInTable = true;
        }
      });
    });

    if (foundInTable && (data.ieltsOverall || data.pteOverall || data.toeflOverall)) {
      return; // Table parsing succeeded — done
    }

    // ── Strategy 1b: Text extraction from section text ───────────────────
    const sectionText = reqContainer.text();
    if (/ielts|pte|toefl/i.test(sectionText)) {
      extractEnglishRequirements(sectionText, data);
      if (data.ieltsOverall || data.pteOverall || data.toeflOverall) return;
    }
  }

  // ── Strategy 2: Full page text fallback ──────────────────────────────────
  // Also scan ALL tables on the page for test type/requirement rows
  let foundInPageTable = false;
  $("table").each((_, table) => {
    if (data.ieltsOverall && data.pteOverall && data.toeflOverall) return false;
    const $table = $(table);
    $table.find("tr").each((_, row) => {
      const cells = $(row).find("td,th");
      if (cells.length < 2) return;
      const testType = $(cells.get(0)!).text().trim();
      const reqText = $(cells.get(1)!).text().trim();
      if (/ielts|pte|toefl|cae|cambridge|duolingo|det|pearson/i.test(testType)) {
        parseEnglishTestCell(testType, reqText, data);
        foundInPageTable = true;
      }
    });
  });

  if (foundInPageTable && (data.ieltsOverall || data.pteOverall || data.toeflOverall)) return;

  // Final fallback: plain text on the full page
  extractEnglishRequirements($("body").text(), data);
}

function extractEnglishRequirements(text: string, data: Partial<CourseData>) {
  const ieltsSection = text.match(/IELTS\s*(?:Academic|academic)?[^]*?(?=(?:TOEF|TOFL|TOFEL|PTE|Cambridge|CAE|Duolingo|Pathway|Credit|Recognition|\n\s*\n))/i);
  const ieltsText = ieltsSection ? ieltsSection[0] : text;

  // Pattern -1 (highest priority): VIT/common format
  // "IELTS Academic: Overall 6.0, with no individual band below 5.5."
  // "IELTS: Overall 7.0, with no band below 6.5"
  // "IELTS Academic: Overall score 6.5, with no band below 6.0"  ← VIT actual format
  if (!data.ieltsOverall) {
    const vitM = ieltsText.match(/IELTS[^:]*:\s*Overall\s+(?:score\s+)?([\d.]+)[^.]*?no\s+(?:individual\s+)?band\s+(?:score\s+)?(?:less\s+than|lower\s+than|below)\s+([\d.]+)/i);
    if (vitM) {
      const overall = parseFloat(vitM[1]);
      const min = parseFloat(vitM[2]);
      if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9) {
        data.ieltsOverall = overall;
        if (!data.ieltsListening) data.ieltsListening = min;
        if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
        if (!data.ieltsWriting) data.ieltsWriting = min;
        if (!data.ieltsReading) data.ieltsReading = min;
      }
    }
  }

  // Pattern 0: "IELTS: 6.5 (no band less than 6.0)" — 3-group spec pattern
  if (!data.ieltsOverall) {
    const noBandLessM = ieltsText.match(/IELTS[:\s]*(?:Academic[:\s]*)?(\d+(?:\.\d+)?)[^\d]+(?:no\s+(?:band|component)\s+(?:less|lower|below)\s*(?:than)?|minimum\s+(?:of\s+)?(?:band|score)?)[:\s]*([\d.]+)/i);
    if (noBandLessM) {
      const overall = parseFloat(noBandLessM[1]);
      const min = parseFloat(noBandLessM[2]);
      if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9) {
        data.ieltsOverall = overall;
        if (!data.ieltsListening) data.ieltsListening = min;
        if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
        if (!data.ieltsWriting) data.ieltsWriting = min;
        if (!data.ieltsReading) data.ieltsReading = min;
      }
    }
  }

  // Pattern: "IELTS 6.5 (6.0 in each band)" — spec's most common compact format
  if (!data.ieltsOverall) {
    const eachBandM = ieltsText.match(/IELTS[:\s]*(?:Academic[:\s]*)?(\d+(?:\.\d+)?)\s*\([\s]*(\d+(?:\.\d+)?)\s*(?:in\s*each|each\s*(?:band|component|skill))/i);
    if (eachBandM) {
      const overall = parseFloat(eachBandM[1]);
      const each = parseFloat(eachBandM[2]);
      if (overall >= 4 && overall <= 9 && each >= 4 && each <= 9) {
        data.ieltsOverall = overall;
        if (!data.ieltsListening) data.ieltsListening = each;
        if (!data.ieltsSpeaking) data.ieltsSpeaking = each;
        if (!data.ieltsWriting) data.ieltsWriting = each;
        if (!data.ieltsReading) data.ieltsReading = each;
      }
    }
  }

  // Pattern: "IELTS 7.0 (L:6.5, R:6.5, W:7.0, S:7.0)" — explicit per-skill breakdown
  if (!data.ieltsOverall) {
    const detailedM = ieltsText.match(/IELTS[:\s]*(?:Academic[:\s]*)?(\d+(?:\.\d+)?)[^(]*\(L(?:istening)?[:\s]*(\d+(?:\.\d+)?)[,\s]+R(?:eading)?[:\s]*(\d+(?:\.\d+)?)[,\s]+W(?:riting)?[:\s]*(\d+(?:\.\d+)?)[,\s]+S(?:peaking)?[:\s]*(\d+(?:\.\d+)?)\)/i);
    if (detailedM) {
      const overall = parseFloat(detailedM[1]);
      if (overall >= 4 && overall <= 9) {
        data.ieltsOverall = overall;
        data.ieltsListening = parseFloat(detailedM[2]);
        data.ieltsReading = parseFloat(detailedM[3]);
        data.ieltsWriting = parseFloat(detailedM[4]);
        data.ieltsSpeaking = parseFloat(detailedM[5]);
      }
    }
  }

  // Pattern: "minimum IELTS overall X.X with no band below X.X" — combined in one phrase
  if (!data.ieltsOverall) {
    const minNoBandM = ieltsText.match(/IELTS[^.]*?(?:minimum|min|overall)\s*(?:score\s*(?:of\s*)?)?([\d.]+)[^.]*?(?:no\s+(?:individual\s+)?(?:band|score|component)[^.]*?(?:below|less\s+than|lower\s+than|under)|minimum\s+(?:of\s+)?(?:band|score|component)\s*(?:of\s*)?)[\s:]*([\d.]+)/i);
    if (minNoBandM) {
      const overall = parseFloat(minNoBandM[1]);
      const min = parseFloat(minNoBandM[2]);
      if (overall >= 4 && overall <= 9 && min >= 4 && min <= 9) {
        data.ieltsOverall = overall;
        if (!data.ieltsListening) data.ieltsListening = min;
        if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
        if (!data.ieltsWriting) data.ieltsWriting = min;
        if (!data.ieltsReading) data.ieltsReading = min;
      }
    }
  }

  const ieltsPatterns = [
    /IELTS\s*(?:Academic|academic)?[:\s]*(?:overall\s*(?:score\s*)?)?(\d+(?:\.\d+)?)/i,
    /IELTS\s*(?:Academic|academic)?[^.]*?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+(?:\.\d+)?)/i,
    /IELTS\s*(?:Academic|academic)?[^.]*?(\d+(?:\.\d+)?)\s*(?:overall|or\s*above|or\s*higher)/i,
    /IELTS\s*(?:Academic|academic)?\s*[\s\S]{0,80}?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+(?:\.\d+)?)/i,
  ];
  for (const p of ieltsPatterns) {
    if (data.ieltsOverall) break;
    const m = ieltsText.match(p);
    if (m) {
      const v = parseFloat(m[1]);
      if (v >= 4 && v <= 9) data.ieltsOverall = v;
    }
  }

  if (data.ieltsOverall) {
    const noBandPatterns = [
      /no\s*(?:band|individual|sub|score|component)[^.]*?(?:below|less\s*than|lower\s*than|under)\s*(\d+(?:\.\d+)?)/i,
      /(?:minimum|min)\s*(?:band|score)\s*(?:of\s*)?(\d+(?:\.\d+)?)/i,
      /(?:each|all|every)\s*(?:band|component|sub)[^.]*?(\d+(?:\.\d+)?)/i,
      /\(no\s*band\s*(?:less\s*than|below)\s*(\d+(?:\.\d+)?)\)/i,
    ];
    for (const p of noBandPatterns) {
      const m = ieltsText.match(p);
      if (m) {
        const min = parseFloat(m[1]);
        if (min >= 4 && min <= 9) {
          if (!data.ieltsListening) data.ieltsListening = min;
          if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
          if (!data.ieltsWriting) data.ieltsWriting = min;
          if (!data.ieltsReading) data.ieltsReading = min;
          break;
        }
      }
    }

    const ieltsSubPatterns: { key: keyof CourseData; pattern: RegExp }[] = [
      { key: "ieltsListening", pattern: /listening[:\s]*(\d+(?:\.\d+)?)/i },
      { key: "ieltsSpeaking", pattern: /speaking[:\s]*(\d+(?:\.\d+)?)/i },
      { key: "ieltsWriting", pattern: /writing[:\s]*(\d+(?:\.\d+)?)/i },
      { key: "ieltsReading", pattern: /reading[:\s]*(\d+(?:\.\d+)?)/i },
    ];
    for (const { key, pattern } of ieltsSubPatterns) {
      const m = ieltsText.match(pattern);
      if (m) {
        const v = parseFloat(m[1]);
        if (v >= 4 && v <= 9) (data as any)[key] = v;
      }
    }
  }

  // TOEFL "with X in each section" combined pattern — "TOEFL iBT: 79 (no section below 18)" / "TOEFL 79 overall with 18 in each section"
  if (!data.toeflOverall) {
    const toeflWithEachM = text.match(/(?:TOEFL|TOFEL)[:\s]*(?:iBT)?[:\s]*(\d+)[^.]*?(?:with|and)\s+(\d+)\s+in\s+(?:each|all)(?:\s+section)?/i);
    if (toeflWithEachM) {
      const overall = parseInt(toeflWithEachM[1]);
      const min = parseInt(toeflWithEachM[2]);
      if (overall >= 30 && overall <= 120 && min >= 0 && min <= 30) {
        data.toeflOverall = overall;
        data.toeflListening = min; data.toeflSpeaking = min;
        data.toeflWriting = min; data.toeflReading = min;
      }
    }
  }

  if (!data.toeflOverall) {
    const toeflNoSectionM = text.match(/(?:TOEFL|TOFEL)[:\s]*(?:iBT)?[:\s]*(\d+)[^)]*?\(no\s*section\s*(?:below|less\s*than)\s*(\d+)\)/i);
    if (toeflNoSectionM) {
      const overall = parseInt(toeflNoSectionM[1]);
      const min = parseInt(toeflNoSectionM[2]);
      if (overall >= 30 && overall <= 120 && min >= 0 && min <= 30) {
        data.toeflOverall = overall;
        data.toeflListening = min; data.toeflSpeaking = min;
        data.toeflWriting = min; data.toeflReading = min;
      }
    }
  }

  const toeflPatterns = [
    /(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?[:\s]*(?:overall\s*(?:score\s*)?(?:of\s*)?)?(\d+)(?:\s*[-–]\s*(\d+))?/i,
    /(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?[^.]*?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+)(?:\s*[-–]\s*(\d+))?/i,
    /(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?[^.]*?(\d+)(?:\s*[-–]\s*(\d+))?\s*(?:overall|or\s*above)/i,
    /(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?\s*[\s\S]{0,80}?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+)(?:\s*[-–]\s*(\d+))?/i,
  ];
  for (const p of toeflPatterns) {
    if (data.toeflOverall) break;
    const m = text.match(p);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 30 && v <= 120) data.toeflOverall = v;
    }
  }

  const toeflSection = text.match(/(?:TOEFL|TOFEL|TOEF[FL])\s*(?:iBT|ibt|IBT)?[^]*?(?=(?:PTE|Cambridge|CAE|Duolingo|Pathway|Credit|Recognition|IELTS|\n\s*\n))/i);
  const toeflText = toeflSection ? toeflSection[0] : "";
  if (data.toeflOverall && toeflText) {
    const toeflSubPatterns: { key: keyof CourseData; pattern: RegExp }[] = [
      { key: "toeflListening", pattern: /listening[:\s]*(\d+)/i },
      { key: "toeflSpeaking", pattern: /speaking[:\s]*(\d+)/i },
      { key: "toeflWriting", pattern: /writing[:\s]*(\d+)/i },
      { key: "toeflReading", pattern: /reading[:\s]*(\d+)/i },
    ];
    for (const { key, pattern } of toeflSubPatterns) {
      const m = toeflText.match(pattern);
      if (m) {
        const v = parseInt(m[1]);
        if (v >= 0 && v <= 30) (data as any)[key] = v;
      }
    }
  }

  if (data.toeflOverall && toeflText) {
    if (!data.toeflListening) {
      const minScoreMatch = toeflText.match(/minimum\s*scores?[:\s]*Reading\s*(\d+)[,\s]*Listening\s*(\d+)[,\s]*Speaking\s*(\d+)[,\s]*Writing\s*(\d+)/i);
      if (minScoreMatch) {
        data.toeflReading = parseInt(minScoreMatch[1]);
        data.toeflListening = parseInt(minScoreMatch[2]);
        data.toeflSpeaking = parseInt(minScoreMatch[3]);
        data.toeflWriting = parseInt(minScoreMatch[4]);
      }
    }
    if (!data.toeflListening) {
      const noBandPatterns = [
        /no\s*(?:section|band|component|skill)[^.]*?(?:below|less\s*than|lower\s*than|under)\s*(\d+)/i,
        /(?:minimum|min)\s*(?:score|band)\s*(?:of\s*)?(\d+)\s*(?:in\s*each|per\s*section)/i,
        /(?:each|all)\s*(?:section|component)[^.]*?(?:minimum|at\s*least)\s*(\d+)/i,
      ];
      for (const p of noBandPatterns) {
        const m = toeflText.match(p);
        if (m) {
          const min = parseInt(m[1]);
          if (min >= 0 && min <= 30) {
            if (!data.toeflListening) data.toeflListening = min;
            if (!data.toeflSpeaking) data.toeflSpeaking = min;
            if (!data.toeflWriting) data.toeflWriting = min;
            if (!data.toeflReading) data.toeflReading = min;
            break;
          }
        }
      }
    }
  }

  // PTE "with X in each" combined pattern — "PTE Academic 58 overall with 50 in each" / "PTE: 58 (no skill below 50)"
  if (!data.pteOverall) {
    const pteWithEachM = text.match(/PTE[:\s]*(?:Academic)?[:\s]*(\d+)[^.]*?(?:with|and)\s+(\d+)\s+in\s+(?:each|all)/i);
    if (pteWithEachM) {
      const overall = parseInt(pteWithEachM[1]);
      const min = parseInt(pteWithEachM[2]);
      if (overall >= 30 && overall <= 90 && min >= 30 && min <= 90) {
        data.pteOverall = overall;
        data.pteListening = min; data.pteSpeaking = min;
        data.pteWriting = min; data.pteReading = min;
      }
    }
  }

  const ptePatterns = [
    /PTE\s*(?:Academic|academic)?[:\s]*(?:overall\s*(?:score\s*)?(?:of\s*)?)?(\d+)/i,
    /PTE\s*(?:Academic|academic)?[^.]*?(?:overall\s*(?:score\s*)?(?:of\s*)?)(\d+)/i,
    /PTE\s*(?:Academic|academic)?[^.]*?(\d+)\s*(?:overall|or\s*above)/i,
  ];
  for (const p of ptePatterns) {
    if (data.pteOverall) break;
    const m = text.match(p);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 30 && v <= 90) data.pteOverall = v;
    }
  }

  const pteSection = text.match(/PTE\s*(?:Academic|academic)?[^]*?(?=(?:TOEF|Cambridge|CAE|Duolingo|Pathway|Credit|Recognition|IELTS|\n\s*\n))/i);
  const pteText = pteSection ? pteSection[0] : "";
  if (data.pteOverall && pteText) {
    const noPteBelow = pteText.match(/no\s*(?:score|band|component|communicative\s*skill)[^.]*?(?:below|less\s*than|lower\s*than|under)\s*(\d+)/i)
      || pteText.match(/(?:each|all)\s*(?:communicative\s*)?skill[^.]*?(?:minimum|at\s*least)\s*(\d+)/i)
      || pteText.match(/(?:minimum|min)[^.]*?(?:in\s+each|per\s+section|per\s+skill)[^.]*?(\d+)/i)
      || pteText.match(/PTE[^.]*?(\d+)\s*\(no\s*skill\s*(?:below|less\s*than)\s*(\d+)\)/i);
    if (noPteBelow) {
      const minStr = noPteBelow[2] ?? noPteBelow[1];
      const min = parseInt(minStr);
      if (min >= 30 && min <= 90) {
        if (!data.pteListening) data.pteListening = min;
        if (!data.pteSpeaking) data.pteSpeaking = min;
        if (!data.pteWriting) data.pteWriting = min;
        if (!data.pteReading) data.pteReading = min;
      }
    }

    // Also try extracting individual PTE skill scores
    if (!data.pteListening || !data.pteSpeaking || !data.pteWriting || !data.pteReading) {
      const pteSubPatterns: { key: keyof CourseData; pattern: RegExp }[] = [
        { key: "pteListening", pattern: /listening[:\s]*(\d+)/i },
        { key: "pteSpeaking", pattern: /speaking[:\s]*(\d+)/i },
        { key: "pteWriting", pattern: /writing[:\s]*(\d+)/i },
        { key: "pteReading", pattern: /reading[:\s]*(\d+)/i },
      ];
      for (const { key, pattern } of pteSubPatterns) {
        const m = pteText.match(pattern);
        if (m && !(data as any)[key]) {
          const v = parseInt(m[1]);
          if (v >= 30 && v <= 90) (data as any)[key] = v;
        }
      }
    }
  }

  const cambridgePatterns = [
    /Cambridge\s*(?:CAE|C1\s*Advanced)?[:\s]*(?:(?:CAE\s*)?score\s*(?:of\s*)?)?(\d+)/i,
    /CAE\s*(?:score\s*(?:of\s*)?)?(\d+)/i,
    /C1\s*Advanced[:\s]*(?:score\s*(?:of\s*)?)?(\d+)/i,
  ];
  for (const p of cambridgePatterns) {
    if (data.cambridgeOverall) break;
    const m = text.match(p);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 140 && v <= 230) data.cambridgeOverall = v;
    }
  }

  const duolingoPatterns = [
    /Duolingo\s*(?:English\s*Test)?[:\s]*(?:overall\s*(?:score\s*)?(?:of\s*)?)?(\d+)/i,
    /DET[:\s]*(?:overall\s*)?(\d+)/i,
  ];
  for (const p of duolingoPatterns) {
    if (data.duolingoOverall) break;
    const m = text.match(p);
    if (m) {
      const v = parseInt(m[1]);
      if (v >= 50 && v <= 160) data.duolingoOverall = v;
    }
  }
}

function normalizeMonth(m: string): string {
  const abbrevMap: Record<string, string> = {
    jan: "January", feb: "February", mar: "March", apr: "April",
    may: "May", jun: "June", jul: "July", aug: "August",
    sep: "September", oct: "October", nov: "November", dec: "December",
  };
  const key = m.toLowerCase().slice(0, 3);
  return abbrevMap[key] || m;
}

function extractIntakeMonths(text: string, data: Partial<CourseData>) {
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const MONTH_RE = /January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec/;

  const intakeMonths: string[] = [];
  const intakeDays: number[] = [];

  // Pass 1: Look for full "day Month" date patterns — "15 February 2025", "20 Jul"
  const fullDatePattern = new RegExp(`\\b(\\d{1,2})\\s+(${MONTH_RE.source})\\b`, "gi");
  let dateMatch: RegExpExecArray | null;
  while ((dateMatch = fullDatePattern.exec(text)) !== null) {
    const day = parseInt(dateMatch[1]);
    const month = normalizeMonth(dateMatch[2]);
    if (day >= 1 && day <= 31 && MONTHS.includes(month)) {
      if (!intakeDays.includes(day)) intakeDays.push(day);
      if (!intakeMonths.includes(month)) intakeMonths.push(month);
    }
  }

  // Pass 1b: "Applications open: February, July" / "Next intake: September 2025" / "Available intakes: March"
  if (intakeMonths.length === 0) {
    const abbrevs = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec";
    const monthNames = MONTHS.join("|");
    const appOpenRe = new RegExp(
      `(?:applications?\\s*(?:open|open\\s*for|close|closing|date|open(?:ing)?\\s*date)|next\\s*(?:available\\s*)?intake|available\\s*intakes?|study\\s*(?:period|periods?|start|begins?)|course\\s*(?:start|commencement)|enrollment\\s*(?:date|period))[:\\s]+([^\\n.]{0,200})`,
      "gi"
    );
    let appM: RegExpExecArray | null;
    while ((appM = appOpenRe.exec(text)) !== null) {
      const chunk = appM[1];
      const found = chunk.match(new RegExp(`\\b(${monthNames}|${abbrevs})\\b`, "gi")) ?? [];
      for (const raw of found) {
        const m = normalizeMonth(raw);
        if (MONTHS.includes(m) && !intakeMonths.includes(m)) intakeMonths.push(m);
      }
    }
  }

  // Pass 2: Context-scoped intake sections
  if (intakeMonths.length === 0) {
    const intakeSections = text.match(/(?:intake|start\s*date|commencement|commence|entry\s*point|intake\s*option)[^]*?(?:\n\n|See\s|$)/gi) ?? [];
    for (const section of intakeSections) {
      for (const m of MONTHS) {
        if (new RegExp(`\\b${m}\\b`, "i").test(section) && !intakeMonths.includes(m)) intakeMonths.push(m);
      }
    }
  }

  // Pass 3: Inline list pattern — "Intake: February, July, November"
  if (intakeMonths.length === 0) {
    const monthNames = MONTHS.join("|");
    const abbrevs = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec";
    const listRe = new RegExp(
      `(?:intake|start|commencement)[^.]{0,100}?((?:(?:${monthNames}|${abbrevs})[\\s,/and]*)+)`,
      "gi",
    );
    let listMatch: RegExpExecArray | null;
    while ((listMatch = listRe.exec(text)) !== null) {
      const found = listMatch[1].match(new RegExp(`\\b(${monthNames}|${abbrevs})\\b`, "gi")) ?? [];
      for (const raw of found) {
        const m = normalizeMonth(raw);
        if (MONTHS.includes(m) && !intakeMonths.includes(m)) intakeMonths.push(m);
      }
    }
  }

  // Pass 4: Semester / Trimester fallback — map to representative months
  if (intakeMonths.length === 0) {
    const semesterMap: [RegExp, string[]][] = [
      [/trimester\s*1/i, ["January", "February"]],
      [/trimester\s*2/i, ["May", "June"]],
      [/trimester\s*3/i, ["September", "October"]],
      [/semester\s*1/i, ["February", "March"]],
      [/semester\s*2/i, ["July", "August"]],
    ];
    for (const [re, months] of semesterMap) {
      if (re.test(text)) {
        for (const m of months) {
          if (!intakeMonths.includes(m)) intakeMonths.push(m);
        }
      }
    }
  }

  if (intakeMonths.length > 0) data.intakeMonths = intakeMonths;
  if (intakeDays.length > 0) data.intakeDays = intakeDays[0]; // store first start day
}

async function analyzeImageWithGemini(imageUrl: string, context: string): Promise<Partial<CourseData>> {
  if (!GEMINI_API_KEY) return {};
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    const resp = await fetch(imageUrl, { signal: controller.signal });
    clearTimeout(timeout);
    if (!resp.ok) return {};
    const buffer = await resp.arrayBuffer();
    const base64 = Buffer.from(buffer).toString("base64");
    const mimeType = resp.headers.get("content-type") || "image/png";

    const prompt = `Extract ALL English language requirements and/or fees from this image. ${context}
Return JSON with ONLY the fields you find:
{"ieltsOverall":<number>,"ieltsListening":<number>,"ieltsSpeaking":<number>,"ieltsWriting":<number>,"ieltsReading":<number>,"pteOverall":<number>,"pteListening":<number>,"pteSpeaking":<number>,"pteWriting":<number>,"pteReading":<number>,"toeflOverall":<number>,"toeflListening":<number>,"toeflSpeaking":<number>,"toeflWriting":<number>,"toeflReading":<number>,"cambridgeOverall":<number>,"duolingoOverall":<number>,"internationalFee":<number>,"currency":"<AUD|GBP|USD>","feeTerm":"<Annual|Trimester|Semester|Term|Session|Per Unit|Full Course>"}
Extract ALL test types: IELTS Academic, TOEFL iBT, PTE Academic, Cambridge CAE/C1 Advanced, Duolingo. Use null for missing fields. Only include INTERNATIONAL student fees.`;

    const body = JSON.stringify({
      contents: [{
        parts: [
          { text: prompt },
          { inline_data: { mime_type: mimeType, data: base64 } },
        ],
      }],
      generationConfig: { responseMimeType: "application/json", maxOutputTokens: 1024 },
    });

    for (const model of GEMINI_MODELS) {
      try {
        const apiResp = await fetch(geminiUrl(model), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
        });
        if (apiResp.status === 429 || apiResp.status === 503 || apiResp.status === 404) continue;
        if (!apiResp.ok) continue;
        const data = await apiResp.json() as any;
        const text = data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
        if (text) return JSON.parse(text) as Partial<CourseData>;
      } catch { continue; }
    }
  } catch {}
  return {};
}

async function extractFeesFromPdf(pdfUrl: string, courseName: string): Promise<Partial<CourseData>> {
  if (!GEMINI_API_KEY) return {};
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    const resp = await fetch(pdfUrl, { signal: controller.signal });
    clearTimeout(timeout);
    if (!resp.ok) return {};
    const ct = resp.headers.get("content-type") || "";
    if (!ct.includes("pdf") && !pdfUrl.toLowerCase().includes(".pdf")) return {};

    const buffer = await resp.arrayBuffer();
    if (buffer.byteLength > 5 * 1024 * 1024) return {};
    const base64 = Buffer.from(buffer).toString("base64");

    const prompt = `Extract the INTERNATIONAL student tuition fee for the course "${courseName}" from this PDF fee schedule.
Return JSON: {"internationalFee":<number per year or per unit>,"currency":"<AUD|GBP|USD>","feeTerm":"<Annual|Trimester|Semester|Term|Session|Per Unit|Full Course>","feeYear":<year>}
Use null for missing fields. Only include INTERNATIONAL fees.`;

    const body = JSON.stringify({
      contents: [{
        parts: [
          { text: prompt },
          { inline_data: { mime_type: "application/pdf", data: base64 } },
        ],
      }],
      generationConfig: { responseMimeType: "application/json", maxOutputTokens: 1024 },
    });

    for (const model of GEMINI_MODELS) {
      try {
        const apiResp = await fetch(geminiUrl(model), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
        });
        if (apiResp.status === 429 || apiResp.status === 503 || apiResp.status === 404) continue;
        if (!apiResp.ok) continue;
        const data = await apiResp.json() as any;
        const text = data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
        if (text) return JSON.parse(text) as Partial<CourseData>;
      } catch { continue; }
    }
  } catch {}
  return {};
}

async function enrichFromRelatedPages(courseData: Partial<CourseData>, relatedPages: { fees?: string; requirements?: string; entry?: string; feesPdf?: string }, html?: string, courseUrl?: string) {
  const needsFees = !courseData.internationalFee;
  const needsAnyEnglish = !(courseData.ieltsOverall && courseData.pteOverall && courseData.toeflOverall && courseData.cambridgeOverall);

  const pagesToFetch: { url: string; type: string }[] = [];

  if (needsFees && relatedPages.fees) pagesToFetch.push({ url: relatedPages.fees, type: "fees" });
  if (needsAnyEnglish && relatedPages.entry) pagesToFetch.push({ url: relatedPages.entry, type: "english" });
  if ((needsFees || needsAnyEnglish) && relatedPages.requirements) pagesToFetch.push({ url: relatedPages.requirements, type: "requirements" });

  for (const page of pagesToFetch) {
    try {
      const pHtml = await fetchPage(page.url);
      const text = cheerio.load(pHtml)("body").text();

      if (page.type === "fees" || page.type === "requirements") {
        if (!courseData.internationalFee) {
          extractInternationalFees(text, courseData);
          if (!courseData.internationalFee) {
            const $pg = cheerio.load(pHtml);
            extractFeeFromHtmlTables($pg, courseData);
          }
        }
      }
      if (page.type === "english" || page.type === "requirements") {
        extractEnglishRequirements(text, courseData);
      }
      if (!courseData.intakeMonths?.length) extractIntakeMonths(text, courseData);
    } catch {}
  }

  if (needsFees && relatedPages.feesPdf && !courseData.internationalFee) {
    try {
      const pdfData = await extractFeesFromPdf(relatedPages.feesPdf, courseData.courseName || "");
      if (pdfData.internationalFee) {
        courseData.internationalFee = pdfData.internationalFee;
        courseData.currency = pdfData.currency || "AUD";
        courseData.feeTerm = pdfData.feeTerm || "Annual";
        courseData.feeYear = pdfData.feeYear || undefined;
      }
    } catch {}
  }

  if (needsAnyEnglish && html && courseUrl) {
    const images = findImageUrls(html, courseUrl);
    for (const imgUrl of images.slice(0, 3)) {
      try {
        const imgData = await analyzeImageWithGemini(imgUrl, `Course: ${courseData.courseName}`);
        let foundAnything = false;
        if (imgData.ieltsOverall && typeof imgData.ieltsOverall === "number" && imgData.ieltsOverall >= 4 && imgData.ieltsOverall <= 9) {
          courseData.ieltsOverall = imgData.ieltsOverall;
          foundAnything = true;
        }
        const numFields = ["ieltsListening", "ieltsSpeaking", "ieltsWriting", "ieltsReading", "pteOverall", "pteListening", "pteSpeaking", "pteWriting", "pteReading", "toeflOverall", "toeflListening", "toeflSpeaking", "toeflWriting", "toeflReading", "cambridgeOverall", "duolingoOverall"] as const;
        for (const f of numFields) {
          const v = imgData[f];
          if (v && typeof v === "number" && v > 0) {
            (courseData as any)[f] = v;
            foundAnything = true;
          }
        }
        if (imgData.internationalFee && typeof imgData.internationalFee === "number" && imgData.internationalFee > 1000 && !courseData.internationalFee) {
          courseData.internationalFee = imgData.internationalFee;
          courseData.currency = imgData.currency || "AUD";
          courseData.feeTerm = imgData.feeTerm || "Annual";
          foundAnything = true;
        }
        if (foundAnything) break;
      } catch {}
    }
  }
}

const BATCH_CLASSIFY_PROMPT = `You are a university course classifier. Given a list of courses with their names and any extracted data, fill in ONLY the missing fields.

Return a JSON array where each item has:
- "index": the original index number
- "category": one of: "Business & Management", "Engineering & Technology", "Computer Science & IT", "Medicine & Health", "Arts, Humanities & Social Sciences", "Education & Social Work", "Architecture, Building & Design", "Media & Communications", "Law & Legal Studies", "Hospitality, Tourism & Events", "Science & Mathematics", "Agriculture & Environmental Science"
- "subCategory": specific sub-category (e.g. "Accounting", "Civil Engineering", "Nursing")
- "degreeLevel": one of: "Bachelor", "Master", "PhD", "Certificate & Diploma", "Graduate Certificate & Diploma", "Associate Degree", "Equivalent" (only if not already provided)
- "description": brief 1-2 sentence description if not already provided (max 200 chars)

Only include fields that are MISSING from the input data. Be concise.`;

async function batchClassify(courses: { index: number; name: string; existing: Partial<CourseData> }[]): Promise<Map<number, Partial<CourseData>>> {
  const result = new Map<number, Partial<CourseData>>();
  if (courses.length === 0) return result;

  const input = courses.map(c => {
    const parts = [`#${c.index}: "${c.name}"`];
    if (c.existing.degreeLevel) parts.push(`level=${c.existing.degreeLevel}`);
    if (c.existing.duration) parts.push(`duration=${c.existing.duration} ${c.existing.durationTerm || ""}`);
    if (c.existing.description) parts.push(`has_desc=yes`);
    return parts.join(", ");
  }).join("\n");

  try {
    const text = await geminiChat(BATCH_CLASSIFY_PROMPT, input, 4096);
    const parsed = JSON.parse(text) as any[];
    for (const item of parsed) {
      if (item.index !== undefined) {
        result.set(item.index, {
          category: item.category || undefined,
          subCategory: item.subCategory || undefined,
          degreeLevel: item.degreeLevel || undefined,
          description: item.description || undefined,
        });
      }
    }
  } catch (err) {
    console.log("Batch classify error:", (err as Error).message);
  }

  return result;
}

const SINGLE_EXTRACT_PROMPT = `Extract course data from this university course page. IMPORTANT RULES:
1. ONLY extract INTERNATIONAL student fees, NEVER domestic/local fees. If a fee table has both "International" and "Domestic" columns, use ONLY the International column value.
2. Look for ALL tab sections (Course Overview, Entry Requirements, Fees, Course Structure etc.) - data may be spread across tabs.
3. For IELTS: "IELTS 6.5 (6.0 in each band)" → ieltsOverall=6.5, all band scores=6.0. "IELTS 7.0 (L:6.5, R:6.5, W:7.0, S:7.0)" → parse each band. "No band below 6.0" → set all bands to 6.0.
4. For intake: look for "Applications open:", "Next intake:", "Commencement:", "Study period starts", semester/trimester start dates.
5. Extract ALL English language tests: IELTS Academic, TOEFL iBT, PTE Academic, Cambridge CAE/C1 Advanced, Duolingo.
6. For fees: if you see a range (e.g. $38,000–$42,000), use the higher value as it's usually the international fee.
7. For feeYear: extract the year the fee applies to (e.g. 2025, 2026) if mentioned.

Return JSON:
{
  "courseName": "<name>",
  "category": "<Business & Management|Engineering & Technology|Computer Science & IT|Medicine & Health|Arts, Humanities & Social Sciences|Education & Social Work|Architecture, Building & Design|Media & Communications|Law & Legal Studies|Hospitality, Tourism & Events|Science & Mathematics|Agriculture & Environmental Science>",
  "subCategory": "<specific>",
  "description": "<max 200 chars>",
  "duration": <number|null>,
  "durationTerm": "<Year|Month|Week>",
  "studyMode": "<On Campus|Online|Blended>",
  "degreeLevel": "<Bachelor|Master|PhD|Certificate & Diploma|Graduate Certificate & Diploma|Associate Degree|Equivalent>",
  "studyLoad": "<Full Time|Part Time>",
  "internationalFee": <INTERNATIONAL fee number only|null>,
  "feeTerm": "<Annual|Trimester|Semester|Term|Session|Per Unit|Full Course>",
  "feeYear": <year number e.g. 2025|null>,
  "currency": "<AUD|GBP|USD|NZD|CAD|SGD|EUR>",
  "ieltsOverall": <number|null>, "ieltsListening": <number|null>, "ieltsSpeaking": <number|null>, "ieltsWriting": <number|null>, "ieltsReading": <number|null>,
  "pteOverall": <number|null>, "pteListening": <number|null>, "pteSpeaking": <number|null>, "pteWriting": <number|null>, "pteReading": <number|null>,
  "toeflOverall": <number|null>, "toeflListening": <number|null>, "toeflSpeaking": <number|null>, "toeflWriting": <number|null>, "toeflReading": <number|null>,
  "cambridgeOverall": <number|null>,
  "duolingoOverall": <number|null>,
  "intakeMonths": ["<full month name>"],
  "academicLevel": "<required education level>",
  "otherRequirement": "<other entry requirements>",
  "scholarship": "<scholarship info if present>"
}
Use null for missing fields. For intakeMonths use full month names (January, February etc.).`;

/**
 * Pre-filters compact text content to sections relevant to a specific data field.
 * Reduces AI token consumption by sending only relevant paragraphs.
 */
function extractRelevantSection(content: string, field: "fees" | "requirements" | "intakes" | "duration" | "campus" | "all"): string {
  if (field === "all") return content.slice(0, 8000);

  const sectionKeywords: Record<string, string[]> = {
    fees: ["fee", "tuition", "cost", "price", "payment", "international", "AUD", "GBP"],
    requirements: ["requirement", "entry", "admission", "IELTS", "TOEFL", "PTE", "academic", "english"],
    intakes: ["intake", "start", "commence", "entry", "semester", "trimester", "month"],
    duration: ["duration", "length", "time", "year", "month", "full-time", "part-time"],
    campus: ["location", "campus", "where", "city", "site", "online"],
  };

  const keywords = sectionKeywords[field] ?? [];
  const lines = content.split("\n");
  const relevantLines: string[] = [];
  let lastRelevant = -10;

  lines.forEach((line, i) => {
    const isRelevant = keywords.some((kw) => new RegExp(kw, "i").test(line));
    if (isRelevant) {
      // Include 2 lines of context around each relevant line
      for (let j = Math.max(0, lastRelevant + 1); j < i; j++) {
        if (i - j <= 2) relevantLines.push(lines[j]);
      }
      relevantLines.push(line);
      lastRelevant = i;
    } else if (i - lastRelevant <= 2) {
      relevantLines.push(line); // trailing context
    }
  });

  const result = relevantLines.join("\n").trim();
  return result.length > 200 ? result.slice(0, 4000) : content.slice(0, 4000);
}

async function extractCourseFromPage(content: string, courseName: string): Promise<CourseData | null> {
  try {
    const text = await geminiChat(SINGLE_EXTRACT_PROMPT, `Course: "${courseName}"\n\n${content}`, 2048);
    const data = JSON.parse(text) as CourseData;
    return data.courseName ? data : null;
  } catch {
    return null;
  }
}

// ── Rule-based page classifier (zero AI, zero network) ───────────────────────
// Replaces the Gemini analyzePage call for the common case.
// Returns same shape as analyzePage so downstream code is unchanged.
function classifyPageByRules(
  html: string,
  url: string
): { pageType: "listing" | "detail" | "unknown"; courseLinks: { url: string; name: string }[]; reason: string } {
  const $ = cheerio.load(html);
  let origin = "";
  try { origin = new URL(url).origin; } catch {}

  // Collect course links from this page
  const seenUrls = new Set<string>();
  const courseLinks: { url: string; name: string }[] = [];
  $("a[href]").each((_, el) => {
    const href = $(el).attr("href") || "";
    const text = $(el).text().trim().replace(/\s+/g, " ");
    if (!text || text.length < 5 || text.length > 180) return;
    try {
      const fullUrl = new URL(href, url).toString();
      if (!fullUrl.startsWith(origin)) return;
      if (seenUrls.has(fullUrl)) return;
      if (isCourseUrl(fullUrl) && !isJunkCourseName(text)) {
        seenUrls.add(fullUrl);
        courseLinks.push({ url: fullUrl, name: text });
      }
    } catch {}
  });

  // Signals for "detail" (single course page)
  const h1 = $("h1").first().text().trim();
  const titleEl = $("title").text().trim();
  const hasDegreeH1 = /\b(bachelor|master|doctor|phd|graduate certificate|graduate diploma|diploma of|certificate [iivx]+|honours|mba|msc|bed|bsc|beng|llb|jd)\b/i.test(h1);
  let urlLooksLikeDetail = false;
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    urlLooksLikeDetail = VALID_COURSE_PATH_PATTERNS.some((p) => p.test(pathname)) && pathname.split("/").filter(Boolean).length >= 2;
  } catch {}

  const bodyText = $("body").text().toLowerCase().slice(0, 12000);
  const hasCourseContent = pageContentLooksLikeCourse(bodyText, h1 || titleEl);

  // DETAIL: degree H1 + URL pattern + limited outbound course links
  if (hasDegreeH1 && urlLooksLikeDetail && courseLinks.length < 6) {
    return { pageType: "detail", courseLinks: [], reason: `H1="${h1.slice(0, 60)}", URL matches course detail pattern` };
  }
  // DETAIL: strong course content + very few outbound course links (user pasted a single course URL)
  if (hasCourseContent && courseLinks.length < 3) {
    return { pageType: "detail", courseLinks: [], reason: `Course content present, only ${courseLinks.length} outbound links` };
  }
  // LISTING: many course links found
  if (courseLinks.length >= 5) {
    return { pageType: "listing", courseLinks, reason: `${courseLinks.length} course links found` };
  }
  // LISTING: has even a few course links and a listing-like title
  if (courseLinks.length > 0 && /\b(courses?|programs?|degrees?|study|undergraduate|postgraduate)\b/i.test(h1 + " " + titleEl)) {
    return { pageType: "listing", courseLinks, reason: `${courseLinks.length} course links + listing title` };
  }
  // Has some course links — treat as listing
  if (courseLinks.length > 0) {
    return { pageType: "listing", courseLinks, reason: `${courseLinks.length} course links found` };
  }
  return { pageType: "unknown", courseLinks: [], reason: "no course links or degree content detected" };
}

const ANALYZE_PROMPT = `Analyze this webpage. Is it a course LISTING page (multiple courses with links), a DETAIL page (single course), or UNKNOWN?

Return JSON:
For LISTING: {"pageType":"listing","courseLinks":[{"url":"<full URL>","name":"<course name>"}],"paginationLinks":["<next page url>"]}
For DETAIL: {"pageType":"detail"}
For UNKNOWN: {"pageType":"unknown"}

Be concise. Only include course links with full URLs.`;

async function analyzePage(content: string): Promise<{ pageType: string; courseLinks?: { url: string; name: string }[]; paginationLinks?: string[] }> {
  const text = await geminiChat(ANALYZE_PROMPT, content, 4096);
  try {
    return JSON.parse(text);
  } catch {
    return { pageType: "unknown" };
  }
}

const CRITICAL_FIELDS: (keyof CourseData)[] = [
  "courseName", "degreeLevel", "duration", "studyMode",
  "internationalFee", "ieltsOverall", "intakeMonths",
];
const IMPORTANT_FIELDS: (keyof CourseData)[] = [
  "category", "durationTerm", "feeTerm", "currency",
  "pteOverall", "toeflOverall", "description",
];

function computeCompleteness(d: CourseData): { score: number; missing: string[] } {
  const missing: string[] = [];
  let filled = 0;
  for (const f of CRITICAL_FIELDS) {
    const v = (d as any)[f];
    const ok = v !== null && v !== undefined && v !== "" && (!Array.isArray(v) || v.length > 0);
    if (ok) filled += 2; else missing.push(f);
  }
  for (const f of IMPORTANT_FIELDS) {
    const v = (d as any)[f];
    const ok = v !== null && v !== undefined && v !== "" && (!Array.isArray(v) || v.length > 0);
    if (ok) filled += 1;
  }
  const maxScore = CRITICAL_FIELDS.length * 2 + IMPORTANT_FIELDS.length;
  return { score: Math.round((filled / maxScore) * 100), missing };
}

function validateAndSanitizeCourseData(courseData: CourseData): string[] {
  const warnings: string[] = [];

  // Validate duration
  if (courseData.duration != null && courseData.durationTerm) {
    const termToYearFactor: Record<string, number> = {
      Year: 1, Month: 1 / 12, Week: 1 / 52, Trimester: 1 / 3, Semester: 1 / 2,
    };
    const factor = termToYearFactor[courseData.durationTerm] ?? 1;
    const durationInYears = courseData.duration * factor;
    if (durationInYears > 10 || durationInYears < 0.25) {
      warnings.push(`Unrealistic duration rejected: ${courseData.duration} ${courseData.durationTerm} (${durationInYears.toFixed(2)} yrs)`);
      courseData.duration = undefined as any;
      courseData.durationTerm = undefined as any;
    }
  }

  // Validate fee range
  if (courseData.internationalFee != null) {
    if (courseData.internationalFee < 1000 || courseData.internationalFee > 200000) {
      warnings.push(`Unusual fee rejected: ${courseData.internationalFee}`);
      courseData.internationalFee = undefined as any;
    }
  }

  // Validate IELTS range
  if (courseData.ieltsOverall != null && (courseData.ieltsOverall < 4 || courseData.ieltsOverall > 9)) {
    warnings.push(`Invalid IELTS overall rejected: ${courseData.ieltsOverall}`);
    courseData.ieltsOverall = undefined as any;
    courseData.ieltsListening = undefined as any;
    courseData.ieltsSpeaking = undefined as any;
    courseData.ieltsWriting = undefined as any;
    courseData.ieltsReading = undefined as any;
  }

  // Validate PTE range
  if (courseData.pteOverall != null && (courseData.pteOverall < 30 || courseData.pteOverall > 90)) {
    warnings.push(`Invalid PTE overall rejected: ${courseData.pteOverall}`);
    courseData.pteOverall = undefined as any;
  }

  // Validate TOEFL range
  if (courseData.toeflOverall != null && (courseData.toeflOverall < 30 || courseData.toeflOverall > 120)) {
    warnings.push(`Invalid TOEFL overall rejected: ${courseData.toeflOverall}`);
    courseData.toeflOverall = undefined as any;
  }

  return warnings;
}

async function stageCourse(courseData: CourseData, uniId: number, jobId: string, job?: ScrapeJob): Promise<boolean> {
  if (!courseData.courseName) return false;

  // Last-resort junk filter — catch event/category/news pages the link collector missed
  if (isJunkCourseName(courseData.courseName)) {
    if (job) addLog(job, "status", { message: `Skipped (junk name): "${courseData.courseName.slice(0, 60)}"`, phase: "validate" });
    else console.log(`[JUNK] Skipping non-course page: "${courseData.courseName}"`);
    return false;
  }

  // Reject pages with no course data at all — likely category/landing pages that slipped through
  const hasDegreeLevel = !!courseData.degreeLevel;
  const hasDuration = !!courseData.duration;
  const hasFee = !!courseData.internationalFee;
  if (!hasDegreeLevel && !hasDuration && !hasFee) {
    if (job) addLog(job, "status", { message: `Skipped (empty: no degree/duration/fee): "${courseData.courseName.slice(0, 60)}"`, phase: "validate" });
    else console.log(`[JUNK] Skipping empty page (no degree/duration/fee): "${courseData.courseName}"`);
    return false;
  }

  // Fee term heuristic: fees ≥ $40,000 that have no explicit periodic label are almost
  // certainly full-course totals, not annual fees. "Annual" at this scale is extremely rare
  // for Australian universities. This catches VIT MBA ($48k), BITS ($51k), etc.
  if (
    courseData.internationalFee &&
    courseData.internationalFee >= 40000 &&
    (!courseData.feeTerm || courseData.feeTerm === "Annual")
  ) {
    courseData.feeTerm = "Full Course";
    console.log(`[HEURISTIC] ${courseData.courseName}: fee $${courseData.internationalFee} ≥ $40k → feeTerm set to Full Course`);
  }

  // Validate and sanitize before staging
  const validationWarnings = validateAndSanitizeCourseData(courseData);
  if (validationWarnings.length > 0) {
    for (const w of validationWarnings) {
      const msg = `[${courseData.courseName.slice(0, 40)}] ${w}`;
      if (job) addLog(job, "status", { message: msg, phase: "validate" });
      else console.log(`[VALIDATE] ${msg}`);
    }
  }

  const dup = await pool.query(
    "SELECT id FROM scraped_courses WHERE scrape_job_id=$1 AND course_name=$2 LIMIT 1",
    [jobId, courseData.courseName],
  );
  if (dup.rows.length > 0) return false;

  const { score: completeness, missing } = computeCompleteness(courseData);

  await db.insert(scrapedCoursesTable).values({
    scrapeJobId: jobId,
    universityId: uniId,
    courseName: courseData.courseName,
    category: courseData.category || null,
    subCategory: courseData.subCategory || null,
    courseWebsite: courseData.courseWebsite || null,
    duration: courseData.duration || null,
    durationTerm: courseData.durationTerm || null,
    studyMode: courseData.studyMode || null,
    degreeLevel: courseData.degreeLevel || null,
    studyLoad: courseData.studyLoad || null,
    language: courseData.language || null,
    description: courseData.description || null,
    otherRequirement: courseData.otherRequirement || null,
    internationalFee: courseData.internationalFee || null,
    feeTerm: courseData.feeTerm || null,
    feeYear: courseData.feeYear || null,
    currency: courseData.currency || null,
    ieltsOverall: courseData.ieltsOverall || null,
    ieltsListening: courseData.ieltsListening || null,
    ieltsSpeaking: courseData.ieltsSpeaking || null,
    ieltsWriting: courseData.ieltsWriting || null,
    ieltsReading: courseData.ieltsReading || null,
    pteOverall: courseData.pteOverall || null,
    pteListening: courseData.pteListening || null,
    pteSpeaking: courseData.pteSpeaking || null,
    pteWriting: courseData.pteWriting || null,
    pteReading: courseData.pteReading || null,
    toeflOverall: courseData.toeflOverall || null,
    toeflListening: courseData.toeflListening || null,
    toeflSpeaking: courseData.toeflSpeaking || null,
    toeflWriting: courseData.toeflWriting || null,
    toeflReading: courseData.toeflReading || null,
    cambridgeOverall: courseData.cambridgeOverall || null,
    duolingoOverall: courseData.duolingoOverall || null,
    intakeMonths: courseData.intakeMonths || null,
    academicLevel: courseData.academicLevel || null,
    academicScore: courseData.academicScore || null,
    scoreType: courseData.scoreType || null,
    academicCountry: courseData.academicCountry || null,
    scholarship: courseData.scholarship || null,
    status: "pending",
    completeness,
    notes: missing.length > 0 ? `Missing: ${missing.join(", ")}` : null,
  });

  return true;
}

async function tryDiscoverApiEndpoints(html: string, pageUrl: string, job: ScrapeJob): Promise<{ url: string; name: string }[] | null> {
  const origin = new URL(pageUrl).origin;
  const apiPatterns = html.match(/["'](\/api\/[^"']+(?:course|program|search)[^"']*)["']/gi) || [];
  const queryParams = new URL(pageUrl).search;

  for (const match of apiPatterns) {
    const apiPath = match.replace(/["']/g, "");
    if (apiPath.includes("autocomplete")) continue;

    const tryUrls = [
      `${origin}${apiPath}${queryParams}`,
      `${origin}${apiPath}?page=0&pageSize=500`,
      `${origin}${apiPath}`,
    ];

    for (const tryUrl of tryUrls) {
      try {
        addLog(job, "status", { message: `Trying hidden API: ${apiPath}...`, phase: "discover" });
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        const resp = await fetch(tryUrl, {
          signal: controller.signal,
          headers: {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": pageUrl,
            "User-Agent": STEALTH_PROFILES[0]["User-Agent"],
          },
        });
        clearTimeout(timeout);

        if (!resp.ok) continue;
        const contentType = resp.headers.get("content-type") || "";
        if (!contentType.includes("json")) continue;

        const data = await resp.json() as any;
        const courses = extractCoursesFromApiResponse(data, origin);

        if (courses.length > 0) {
          addLog(job, "status", { message: `API returned ${courses.length} courses. Checking for more pages...`, phase: "discover" });

          const totalPages = data?.result?.totalPage ?? data?.totalPage ?? data?.totalPages ?? 1;

          if (totalPages > 1) {
            for (let page = 1; page < totalPages; page++) {
              try {
                const pageUrlObj = new URL(tryUrl);
                pageUrlObj.searchParams.set("pageQ", String(page));
                const origParams = new URL(pageUrl).searchParams;
                const pageId = origParams.get("PageId");
                if (pageId && !pageUrlObj.searchParams.has("PageId")) pageUrlObj.searchParams.set("PageId", pageId);

                const pResp = await fetch(pageUrlObj.toString(), {
                  headers: {
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": pageUrl,
                    "User-Agent": STEALTH_PROFILES[0]["User-Agent"],
                  },
                });
                if (pResp.ok) {
                  const pData = await pResp.json() as any;
                  courses.push(...extractCoursesFromApiResponse(pData, origin));
                  addLog(job, "status", { message: `Fetched page ${page + 1}/${totalPages} (${courses.length} total courses)`, phase: "discover" });
                }
              } catch {}
              await new Promise((r) => setTimeout(r, 300));
            }
          }
          return courses;
        }
      } catch {}
    }
  }
  return null;
}

function extractCoursesFromApiResponse(data: any, origin: string): { url: string; name: string }[] {
  const courses: { url: string; name: string }[] = [];
  const seen = new Set<string>();

  function findItems(obj: any): any[] {
    if (!obj || typeof obj !== "object") return [];
    if (Array.isArray(obj)) {
      if (obj.length > 0 && (obj[0]?.header || obj[0]?.name || obj[0]?.title || obj[0]?.courseName) && (obj[0]?.link || obj[0]?.url || obj[0]?.href)) return obj;
      for (const item of obj) {
        const found = findItems(item);
        if (found.length > 0) return found;
      }
      return [];
    }
    for (const key of Object.keys(obj)) {
      if (key === "facets" || key === "filters") continue;
      const found = findItems(obj[key]);
      if (found.length > 0) return found;
    }
    return [];
  }

  const items = findItems(data);
  for (const item of items) {
    const name = item.header || item.name || item.title || item.courseName || "";
    let url = item.link?.href || item.url || item.href || item.link?.url || "";
    if (name && url) {
      try {
        const fullUrl = url.startsWith("http") ? url : new URL(url, origin).toString();
        if (!seen.has(fullUrl)) {
          seen.add(fullUrl);
          courses.push({ url: fullUrl, name: name.replace(/<[^>]*>/g, "").trim() });
        }
      } catch {}
    }
  }
  return courses;
}

const JUNK_LINK_NAMES = new Set([
  "courses", "programs", "programme", "programmes", "course", "program",
  "home", "about", "contact", "apply", "admissions", "admission",
  "overview", "search", "find", "browse", "explore", "view all",
  "see all", "learn more", "read more", "more info", "click here",
  "back", "next", "previous", "menu", "nav", "navigation",
  "undergraduate", "postgraduate", "research", "international",
  "domestic", "student", "students", "staff", "alumni", "news",
  "events", "blog", "faq", "help", "support", "privacy", "terms",
  "cookie", "sitemap", "login", "sign in", "register",
  "coursework", "orientation", "handbook", "timetable", "calendar",
  "accommodation", "scholarships", "fees", "tuition", "pathways",
  "about us", "campus", "library", "online", "apply now",
  "student life", "career", "careers", "exchange", "study abroad",
  "research centres", "institutes", "faculty", "school", "department",
  "moving to", "high school", "non-school", "sport", "sports",
  "favourites", "my list", "compare",
  // Standalone category / program-family names (not individual course names)
  "vocational", "elicos", "bits", "mits", "bbus", "course list",
  "english", "english language", "english courses",
]);

const DEGREE_QUALIFIERS = [
  "bachelor", "master", "doctor", "graduate", "diploma", "certificate",
  "phd", "mba", "associate", "honours", "juris", "combined", "double",
  "integrated", "coursework",
];

function urlLastSegmentHasDegreeQualifier(url: string): boolean {
  try {
    const pathname = new URL(url).pathname.toLowerCase();

    // Fast-path: full-path matches a strong course detail pattern (e.g. /courses/bachelor-of-X)
    if (VALID_COURSE_PATH_PATTERNS.some((p) => p.test(pathname))) {
      // Still reject known junk suffixes
      const lastSeg = pathname.split("/").filter(Boolean).pop()?.replace(/\?.*$/, "") || "";
      if (/(scholarships?|info-night|open-day|event|news|fair|expo|community|hub|keydates?|key-dates?)$/.test(lastSeg)) return false;
      return true;
    }

    const lastSeg = pathname.split("/").filter(Boolean).pop()?.replace(/\?.*$/, "") || "";
    if (!DEGREE_QUALIFIERS.some((q) => lastSeg.startsWith(q + "-") || lastSeg === q)) return false;
    // Reject degree-qualified URLs that are clearly info/category pages, not actual course detail pages
    // e.g. phd-scholarships, phd-jobs-and-internships, integrated-masters (category), master-classes
    if (/(scholarships?|jobs?|internships?|employment|career|life|accommodation|sport|news|event|blog|faq|help|support|overview|guide|information|handbook|tips|process|pathway|pathways?|class(?:es)?|fair|expo|hub|community|connect|network|info-night|open-day|keydates?|key-dates?)$/.test(lastSeg)) return false;
    return true;
  } catch { return false; }
}

/**
 * Junk course name patterns — event pages, category pages, news articles.
 * Returns true when the name is clearly NOT a real course.
 */
function isJunkCourseName(name: string): boolean {
  const lower = name.toLowerCase().trim();

  // Basic sanity checks
  if (JUNK_LINK_NAMES.has(lower)) return true;
  if (lower.length < 6) return true;
  if (lower.length > 200) return true;
  if (!/[a-z]/i.test(lower)) return true;
  if (/^(all|view|see|find|browse|search|show)\s/i.test(lower)) return true;
  if (/^(our|the|a)\s+(course|program|degree)/i.test(lower)) return true;
  if (/^(accommodation|sport|scholarships?|fees?|pathways?|exchange|library|campus|career|alumni|research|faculty|department|school|international students?|domestic students?|high school|non.school|postgraduate students?|indigenous|disability|fees? and |student life|moving to|uow \w+)$/i.test(lower)) return true;

  // Event / news / category page patterns
  const junkPatterns = [
    /\binfo\s+night\b/,
    /\bvirtual\s+info\s+night\b/,
    /\bopen\s+day\b/,
    /\bwebinar\b/,
    /\bseminar\b/,
    /\binformation\s+(session|night|event)\b/,
    /^double\s+degrees?$/,
    /^dual\s+degrees?$/,
    /^graduate\s+certificates?$/,
    /^postgraduate\s+courses?$/,
    /^undergraduate\s+courses?$/,
    /^all\s+courses?$/,
    /^(?:our\s+)?courses?$/,
    /^courses?\s+(list|listview|grid|tile|finder|overview|index)$/,
    /^(programs?|degrees?|study)\s+(list|listview|grid|tile|finder|overview|index)$/,
    /^(?:browse|explore|find|view)\s+(?:our\s+)?(?:courses?|programs?|degrees?)$/,
    /retains?\s+tier/,
    /\brackings?\b.*\bspot\b/,
    /\baccredited\b$/,
    /\bwhy\s+choose\b/,
    /^apply\s+now$/,
    /\bnews\b.*\barticle\b/,
    /\bpress\s+release\b/,
    // Key dates / intake dates pages — not actual courses
    /\bkey[\s_-]?dates?\b/,
    /\bkeydates?\b/,
    /\bdomestic[\s_-]keydates?\b/,
    /\bint(?:ernational)?[\s_-]keydates?\b/,
    /\bintake[\s_-]dates?\b/,
  ];
  return junkPatterns.some((p) => p.test(lower));
}

function pageContentLooksLikeCourse(text: string, name?: string): boolean {
  // Check name first — reject obvious junk titles immediately
  if (name && isJunkCourseName(name)) return false;

  const lower = text.slice(0, 8000).toLowerCase();

  // Strong explicit rejection: event/news pages have these but no course data
  if (/\b(info\s+night|virtual\s+info\s+night|open\s+day|info\s+session)\b/.test(lower) &&
    !/\b(ielts|pte|toefl|tuition|duration|credit\s+points?|entry\s+requirements?)\b/.test(lower)) {
    return false;
  }

  const indicators = [
    /\b(ielts|toefl|pte|english proficiency|duolingo|cambridge|language requirement)\b/,
    /\b(tuition fee|annual fee|per year|international fee|course fee|total fee|indicative fee|estimated fee)\b/,
    /\b(duration|years? full.time|years? part.time|credit points?|credit hours?|units? of study|course length)\b/,
    /\b(entry requirements?|admission requirements?|academic requirements?|prerequisite|minimum gpa|minimum grade)\b/,
    /\b(bachelor of|master of|doctor of|graduate certificate|graduate diploma|honours degree|associate degree|diploma of)\b/,
    /\b(course structure|course overview|what you.ll study|learning outcomes|career outcomes|graduate outcomes)\b/,
    /\b(intakes?|start dates?|commence|enrolment|apply now|how to apply|application deadline)\b/,
    /\b(on campus|online|blended|distance learning|study mode|delivery mode)\b/,
  ];
  const matches = indicators.filter((r) => r.test(lower)).length;

  // Threshold: 2+ indicators → valid; 1 + degree keyword in text → valid
  if (matches >= 2) return true;
  if (matches >= 1) {
    const hasDegreeTitle = /\b(bachelor|master|doctor|phd|graduate|diploma|certificate|mba|msc|bed|bsc|ba|bbus|llb|lld|jd|mphil|juris)\b/.test(lower);
    return hasDegreeTitle;
  }
  return false;
}

interface ResearchResult {
  links: { url: string; name: string }[];
  validSamples: number;
  rejectedSamples: number;
  validExamples: string[];
  rejectedExamples: string[];
}

async function researchAndValidateCourseLinks(
  candidates: { url: string; name: string }[],
  job: ScrapeJob
): Promise<ResearchResult> {
  if (candidates.length === 0) return { links: [], validSamples: 0, rejectedSamples: 0, validExamples: [], rejectedExamples: [] };

  // Phase 1: URL-based pre-filter (instant, zero cost)
  const urlFiltered = candidates.filter((c) => urlLastSegmentHasDegreeQualifier(c.url));
  const urlFilterRatio = urlFiltered.length / candidates.length;

  // Decide which list to sample from — use URL-filtered when confident, otherwise all candidates
  const workingList = (urlFilterRatio > 0.4 && urlFiltered.length >= 5) ? urlFiltered : candidates;
  const removedByUrl = candidates.length - workingList.length;
  if (removedByUrl > 0) {
    addLog(job, "status", {
      message: `URL analysis: ${workingList.length} candidate course pages identified, filtered out ${removedByUrl} non-course URLs`,
      phase: "discover",
    });
  }

  // Phase 2: Content sampling — always sample to validate and show real counts to the user
  const sampleSize = Math.min(12, workingList.length);
  const step = Math.max(1, Math.floor(workingList.length / sampleSize));
  const sample: { url: string; name: string }[] = [];
  for (let i = 0; i < workingList.length; i += step) {
    if (sample.length >= sampleSize) break;
    sample.push(workingList[i]);
  }

  addLog(job, "status", {
    message: `Phase 2: Researching ${workingList.length} candidates — sampling ${sample.length} pages to confirm genuine course pages...`,
    phase: "discover",
  });

  const validUrlPrefixes: string[] = [];
  const validUrlDepths: number[] = [];
  const validExamples: string[] = [];
  const rejectedExamples: string[] = [];
  let confirmedCourses = 0;
  let confirmedNonCourses = 0;

  // Fetch all samples in parallel (up to 8 concurrent)
  const sampleSem = makeSemaphore(8);
  await Promise.all(sample.map((candidate) =>
    sampleSem(async () => {
      try {
        // Short-circuit on known junk names before even fetching
        if (isJunkCourseName(candidate.name)) {
          confirmedNonCourses++;
          if (rejectedExamples.length < 3) rejectedExamples.push(candidate.name);
          addLog(job, "status", { message: `✗ Junk page (name filter): "${candidate.name}"`, phase: "discover", sampleResult: "rejected" });
          return;
        }

        const pageHtml = await fetchPage(candidate.url);
        const $ = cheerio.load(pageHtml);
        const bodyText = $("body").text();

        // Fast-path: full-path course URL structure + degree keyword in page <h1> or <title> = auto-accept
        // This prevents Torrens /courses/bachelor-of-X pages from being rejected on minimal content
        const pageTitle = ($("h1").first().text() || $("title").text() || "").trim();
        const urlPathFits = (() => {
          try { return VALID_COURSE_PATH_PATTERNS.some((p) => p.test(new URL(candidate.url).pathname.toLowerCase())); }
          catch { return false; }
        })();
        const titleHasDegree = /\b(bachelor|master|doctor|phd|graduate|diploma|certificate|mba|msc|bed|bsc|beng|llb|jd|juris|honours|associate)\b/i.test(pageTitle);
        if (urlPathFits && titleHasDegree) {
          confirmedCourses++;
          if (validExamples.length < 4) validExamples.push(candidate.name);
          const pathParts = new URL(candidate.url).pathname.split("/").filter(Boolean);
          validUrlDepths.push(pathParts.length);
          if (pathParts.length > 1) validUrlPrefixes.push("/" + pathParts.slice(0, -1).join("/") + "/");
          addLog(job, "status", { message: `✓ Confirmed course (URL+title fast-path): "${candidate.name}"`, phase: "discover", sampleResult: "valid" });
          return;
        }

        const isRealCourse = pageContentLooksLikeCourse(bodyText, candidate.name);

        if (isRealCourse) {
          confirmedCourses++;
          if (validExamples.length < 4) validExamples.push(candidate.name);
          const pathParts = new URL(candidate.url).pathname.split("/").filter(Boolean);
          validUrlDepths.push(pathParts.length);
          if (pathParts.length > 1) {
            validUrlPrefixes.push("/" + pathParts.slice(0, -1).join("/") + "/");
          }
          addLog(job, "status", { message: `✓ Confirmed course: "${candidate.name}"`, phase: "discover", sampleResult: "valid" });
        } else {
          confirmedNonCourses++;
          if (rejectedExamples.length < 3) rejectedExamples.push(candidate.name);
          addLog(job, "status", { message: `✗ Not a course page: "${candidate.name}"`, phase: "discover", sampleResult: "rejected" });
        }
      } catch {}
    })
  ));

  const successRate = sample.length > 0 ? confirmedCourses / sample.length : 0;
  addLog(job, "status", {
    message: `Research complete: ${confirmedCourses}/${sample.length} sampled pages are genuine course pages`,
    phase: "discover",
  });

  if (confirmedCourses === 0) {
    // If URL filter found high-confidence candidates, trust it and proceed with a warning
    if (urlFiltered.length >= 5) {
      addLog(job, "status", {
        message: `⚠ WARNING: Content validation failed for all ${sample.length} samples, but URL analysis found ${urlFiltered.length} degree-qualified URLs. Proceeding with URL-filtered list — manual review recommended.`,
        phase: "discover",
      });
      return { links: urlFiltered, validSamples: 0, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };
    }
    // No URL candidates either — genuinely stuck
    addLog(job, "status", {
      message: `⚠ WARNING: Could not confirm any course pages (0/${sample.length} passed content check, ${urlFiltered.length} URL-filtered candidates). Using all URL-filtered candidates. Check if the university's course pages match expected patterns.`,
      phase: "discover",
    });
    return { links: workingList, validSamples: 0, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };
  }

  if (validUrlDepths.length === 0) return { links: workingList, validSamples: confirmedCourses, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };

  const avgDepth = Math.round(validUrlDepths.reduce((a, b) => a + b, 0) / validUrlDepths.length);

  // Collect ALL confirmed prefixes (not just the most common one).
  // Using the most common prefix kills diversity — e.g. if 7/9 confirmed are /mba/ and
  // 2/9 are /bbus/, using /mba/ as bestPrefix would silently drop all bachelor courses.
  const prefixSet = new Set(validUrlPrefixes);

  // When ALL sampled pages passed (100% success rate), trust the research completely —
  // skip depth/prefix filtering entirely, since all variation is real.
  if (successRate >= 1.0) {
    addLog(job, "status", {
      message: `All ${sample.length} sampled pages confirmed — skipping URL prefix filter to preserve multi-category courses (${workingList.length} total).`,
      phase: "discover",
    });
    return { links: workingList, validSamples: confirmedCourses, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };
  }

  // Partial success: filter, but accept a URL if it matches ANY confirmed prefix (not just the most popular one)
  const filtered = workingList.filter((c) => {
    try {
      const pathParts = new URL(c.url).pathname.split("/").filter(Boolean);
      if (Math.abs(pathParts.length - avgDepth) > 1) return false;
      // Accept if URL matches any confirmed prefix, or no prefixes were detected
      if (prefixSet.size > 0) {
        const urlLower = c.url.toLowerCase();
        const matchesAnyPrefix = [...prefixSet].some((p) => urlLower.includes(p.slice(0, -1)));
        if (!matchesAnyPrefix) return false;
      }
      return true;
    } catch { return false; }
  });

  const removedCount = workingList.length - filtered.length;
  if (removedCount > 0) {
    addLog(job, "status", {
      message: `Filtered out ${removedCount} non-course pages. Will fetch ${filtered.length} validated course pages.`,
      phase: "discover",
    });
  }

  const finalLinks = filtered.length >= 3 ? filtered : workingList;
  return { links: finalLinks, validSamples: confirmedCourses, rejectedSamples: confirmedNonCourses, validExamples, rejectedExamples };
}

// Full-path patterns that strongly indicate a single course detail page
// e.g. torrens.edu.au/courses/bachelor-of-cybersecurity
const VALID_COURSE_PATH_PATTERNS = [
  /\/courses?\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/study\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/programs?\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/degrees?\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/[a-z]+-courses?\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/postgraduate\/[a-z0-9][a-z0-9-]+\/?$/,
  /\/undergraduate\/[a-z0-9][a-z0-9-]+\/?$/,
];

function isCourseUrl(urlStr: string): boolean {
  const lower = urlStr.toLowerCase();

  // Explicit exclusions — these are never course pages
  const excludePatterns = [
    "/accommodation", "/student-life", "/campus-life", "/campus-map", "/campus-tour",
    "/apply", "/application", "/contact", "/about-us", "/about/", "/news/", "/events/",
    "/search", "/category/", "/tag/", "/blog/", "/staff/", "/faculty-profile",
    "/research/", "/library/", "/scholarships", "/support/", "/services/",
    "/node/", "/page/", "/generic/", "/media/", "/documents/", "/resources/",
    "/student-support", "/international-students/visa", "/fees-scholarships",
    "/why-choose", "/info-night", "/open-day", "/virtual-info",
    "/keydates", "/key-dates", "domestic-keydates", "int-keydates",
    // Listing / index pages (not individual courses)
    "/courses-list", "/courses-listview", "/courses-grid", "/courses-tile",
    "/programs-list", "/program-list", "/course-list", "/course-finder",
    "/find-a-course", "/all-courses", "/browse-courses", "/explore-courses",
  ];
  if (excludePatterns.some((p) => lower.includes(p))) return false;
  // Exclude URLs whose last path segment ends with known junk suffixes
  const lastSeg = lower.split("/").filter(Boolean).pop()?.replace(/\?.*$/, "") || "";
  if (/(scholarships?|jobs?(-and-internships?)?|internships?|employment|student-life|community|connect|network|hub|fair|expo|overview|handbook|tips|guide|pathway|pathways?|classes?|info-night|open-day)$/.test(lastSeg)) return false;
  // Exclude listing/index page segments
  if (/^(courses?|programs?|degrees?|study)([- _](list|listview|grid|tile|finder|index|all|browse|explore))?$/.test(lastSeg)) return false;
  if (/^(our[- _])?(courses?|programs?|degrees?)$/.test(lastSeg)) return false;

  // Strong positive: full-path matches a known course detail URL structure
  try {
    const pathname = new URL(urlStr).pathname.toLowerCase();
    if (VALID_COURSE_PATH_PATTERNS.some((p) => p.test(pathname))) return true;
  } catch {}

  return (
    lower.includes("/course") || lower.includes("/program") ||
    lower.includes("/bachelor") || lower.includes("/master") ||
    lower.includes("/diploma") || lower.includes("/study/") ||
    lower.includes("/graduate-certificate") || lower.includes("/graduate-diploma") ||
    lower.includes("/certificate") || lower.includes("/degree") ||
    lower.includes("/phd") || lower.includes("/mba") ||
    lower.includes("/doctorate") || lower.includes("/doctoral") ||
    lower.includes("/undergraduate") || lower.includes("/postgraduate") ||
    lower.includes("/associate-degree") || lower.includes("/double-degree") ||
    lower.includes("/dual-degree") || lower.includes("/juris-doctor") ||
    lower.includes("/honours") || lower.includes("/pathway")
  );
}

function isCourseText(text: string): boolean {
  return /\b(bachelor|master|graduate\s*diploma|diploma|certificate|doctor|phd|mba|associate)\b/i.test(text) ||
    /\b(ba|bsc|ma|msc|mba|bed|beng|llb|med)\b/i.test(text);
}

function sitemapLocToCourseName(loc: string): string {
  const pathParts = new URL(loc).pathname.split("/").filter(Boolean);
  return pathParts[pathParts.length - 1]
    .replace(/\?.*$/, "")
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

function isNestedSitemapLoc(loc: string): boolean {
  return /sitemap/i.test(loc) || loc.endsWith(".xml");
}

function normalizeSitemapUrl(loc: string): string {
  try {
    const u = new URL(loc);
    const DROP_PARAMS = ["students", "audience", "mode", "view", "tab", "ref"];
    DROP_PARAMS.forEach((p) => u.searchParams.delete(p));
    if (!u.search) u.search = "";
    // Site-specific path rewrites for known-broken sitemap entries.
    // VU's sitemap publishes Drupal-multisite legacy paths (/site-N/courses/...) that all 404;
    // the canonical public path is /courses/<slug>.
    if (u.hostname.endsWith("vu.edu.au")) {
      u.pathname = u.pathname.replace(/^\/site-\d+\/courses\//i, "/courses/");
    }
    return u.toString();
  } catch {
    return loc;
  }
}

async function fetchAndParseSitemapForCourses(sitemapUrl: string, seen: Set<string>): Promise<{ url: string; name: string }[]> {
  const courses: { url: string; name: string }[] = [];
  try {
    const content = await fetchPage(sitemapUrl);
    if (!content.includes("<urlset") && !content.includes("<sitemapindex")) return courses;
    const locs = [...content.matchAll(/<loc>([^<]+)<\/loc>/gi)].map((m) => m[1].trim());
    for (const rawLoc of locs) {
      const loc = normalizeSitemapUrl(rawLoc);
      if (seen.has(loc)) continue;
      if (isNestedSitemapLoc(loc)) continue;
      if (isCourseUrl(loc)) {
        seen.add(loc);
        const name = sitemapLocToCourseName(loc);
        if (!isJunkCourseName(name)) {
          courses.push({ url: loc, name });
        }
      }
    }
  } catch {}
  return courses;
}

async function discoverCourseLinksFromSitemap(origin: string, job: ScrapeJob): Promise<{ url: string; name: string }[]> {
  const courses: { url: string; name: string }[] = [];
  const seen = new Set<string>();

  const sitemapIndexUrls = [`${origin}/sitemap.xml`, `${origin}/sitemap_index.xml`];

  for (const smUrl of sitemapIndexUrls) {
    try {
      const xml = await fetchPage(smUrl);
      if (!xml.includes("<")) continue;

      const allLocs = [...xml.matchAll(/<loc>([^<]+)<\/loc>/gi)].map((m) => m[1].trim());

      const nestedSitemaps = allLocs.filter((loc) => isNestedSitemapLoc(loc));

      if (nestedSitemaps.length > 0) {
        addLog(job, "status", { message: `Sitemap index: checking ${nestedSitemaps.length} sub-sitemaps...`, phase: "discover" });
        for (const nestedUrl of nestedSitemaps) {
          if (seen.has(nestedUrl)) continue;
          seen.add(nestedUrl);
          const found = await fetchAndParseSitemapForCourses(nestedUrl, seen);
          if (found.length > 0) {
            addLog(job, "status", { message: `Sub-sitemap ${nestedUrl.split("/").slice(-2).join("/")} → ${found.length} courses`, phase: "discover" });
            courses.push(...found);
          }
        }
      }

      for (const loc of allLocs) {
        if (seen.has(loc) || isNestedSitemapLoc(loc)) continue;
        if (isCourseUrl(loc)) {
          seen.add(loc);
          const name = sitemapLocToCourseName(loc);
          if (!isJunkCourseName(name)) {
            courses.push({ url: loc, name });
          }
        }
      }

      if (courses.length > 0) break;
    } catch {}
  }

  if (courses.length > 0) {
    addLog(job, "status", { message: `Sitemap: found ${courses.length} course URLs total`, phase: "discover" });
  }
  return courses;
}

async function crawlForCourseLinks(startUrl: string, origin: string, job: ScrapeJob, maxDepth = 2): Promise<{ url: string; name: string }[]> {
  const courses: { url: string; name: string }[] = [];
  const seen = new Set<string>();
  const visited = new Set<string>();
  const queue: { url: string; depth: number }[] = [{ url: startUrl, depth: 0 }];

  while (queue.length > 0) {
    const { url: currentUrl, depth } = queue.shift()!;
    if (visited.has(currentUrl) || depth > maxDepth) continue;
    visited.add(currentUrl);

    if (job.stopped) break;

    try {
      const html = await fetchPage(currentUrl);
      const $ = cheerio.load(html);

      $("a[href]").each((_, el) => {
        const href = $(el).attr("href") || "";
        const text = $(el).text().trim().replace(/\s+/g, " ");
        try {
          const fullUrl = new URL(href, origin).toString();
          if (!fullUrl.startsWith(origin)) return;
          if (seen.has(fullUrl)) return;

          const lower = fullUrl.toLowerCase();

          if (isCourseUrl(lower) && !isJunkCourseName(text)) {
            seen.add(fullUrl);
            courses.push({ url: fullUrl, name: text });
          } else if (isCourseText(text) && !isJunkCourseName(text)) {
            seen.add(fullUrl);
            courses.push({ url: fullUrl, name: text });
          } else if (
            depth < maxDepth &&
            fullUrl.startsWith(origin) &&
            !visited.has(fullUrl) &&
            (lower.includes("/study") || lower.includes("/course") || lower.includes("/program") ||
             lower.includes("/academ") || lower.includes("/facult") || lower.includes("/school") ||
             lower.includes("/department") || lower.includes("/undergrad") || lower.includes("/postgrad"))
          ) {
            queue.push({ url: fullUrl, depth: depth + 1 });
          }
        } catch {}
      });

      if (depth > 0 && courses.length > 0) {
        addLog(job, "status", { message: `Crawl depth ${depth}: found ${courses.length} course links so far...`, phase: "discover" });
      }
    } catch {}

    if (courses.length > 300) break;
    if (visited.size > 50) break;
  }

  return courses;
}

async function discoverAllCourseLinks(
  url: string,
  html: string | null,
  job: ScrapeJob,
  aiLinks: { url: string; name: string }[]
): Promise<{ url: string; name: string }[]> {
  const origin = new URL(url).origin;
  const seen = new Set<string>();
  const allCourses: { url: string; name: string }[] = [];

  for (const link of aiLinks) {
    if (!isJunkCourseName(link.name) && !seen.has(link.url)) {
      seen.add(link.url);
      allCourses.push(link);
    }
  }

  if (html) {
    const $ = cheerio.load(html);
    $("a[href]").each((_, el) => {
      const href = $(el).attr("href") || "";
      const text = $(el).text().trim().replace(/\s+/g, " ");
      try {
        const fullUrl = new URL(href, origin).toString();
        if (!fullUrl.startsWith(origin) || seen.has(fullUrl)) return;

        if ((isCourseUrl(fullUrl) || isCourseText(text)) && !isJunkCourseName(text)) {
          seen.add(fullUrl);
          allCourses.push({ url: fullUrl, name: text });
        }
      } catch {}
    });
  }

  // NOTE: Sitemap is now handled in the main flow (researchAndValidateCourseLinks)
  // Do not call discoverCourseLinksFromSitemap here to avoid duplicate work

  if (allCourses.length < 5 && html) {
    addLog(job, "status", { message: "Few courses found, crawling sub-pages for more...", phase: "discover" });
    const crawled = await crawlForCourseLinks(url, origin, job, 2);
    for (const c of crawled) {
      if (!seen.has(c.url)) {
        seen.add(c.url);
        allCourses.push(c);
      }
    }
  }

  return allCourses;
}

async function followPaginatedListing(
  listingUrl: string,
  firstPageHtml: string,
  job: ScrapeJob,
  initialLinks: { url: string; name: string }[]
): Promise<{ url: string; name: string }[]> {
  const origin = new URL(listingUrl).origin;
  const seen = new Set<string>(initialLinks.map((l) => l.url));
  const allCourses: { url: string; name: string }[] = [...initialLinks];

  const $ = cheerio.load(firstPageHtml);

  const totalText = $("body").text().match(/showing\s+[\d,]+\s*[-–]\s*[\d,]+\s+of\s+([\d,]+)/i);
  const totalCount = totalText ? parseInt(totalText[1].replace(/,/g, "")) : 0;

  const nextLinks: Set<string> = new Set();

  $("a[href], link[rel='next']").each((_, el) => {
    const rel = $(el).attr("rel") || "";
    const href = $(el).attr("href") || "";
    if (rel === "next" && href) {
      try { nextLinks.add(new URL(href, origin).toString()); } catch {}
    }
  });

  if (nextLinks.size === 0) {
    const base = new URL(listingUrl);
    const pageParam = base.searchParams.get("page") || base.searchParams.get("pg") ||
      base.searchParams.get("p") || base.searchParams.get("offset");
    const perPage = initialLinks.length || 10;

    if (totalCount > perPage) {
      const totalPages = Math.ceil(totalCount / perPage);
      const limitPages = Math.min(totalPages, 100);
      addLog(job, "status", { message: `Detected ${totalCount} total courses across ~${totalPages} pages. Following pagination...`, phase: "discover" });

      for (let p = 2; p <= limitPages; p++) {
        if (job.stopped) break;

        let pageUrl = "";
        const pathPageMatch = listingUrl.match(/(.+\/page\/)(\d+)(\/?.*)$/);
        if (pathPageMatch) {
          pageUrl = `${pathPageMatch[1]}${p}${pathPageMatch[3]}`;
        } else if (base.searchParams.has("page")) {
          const u = new URL(listingUrl);
          u.searchParams.set("page", String(p));
          pageUrl = u.toString();
        } else if (base.searchParams.has("pg")) {
          const u = new URL(listingUrl);
          u.searchParams.set("pg", String(p));
          pageUrl = u.toString();
        } else if (base.searchParams.has("start") || base.searchParams.has("offset")) {
          const u = new URL(listingUrl);
          const param = base.searchParams.has("start") ? "start" : "offset";
          u.searchParams.set(param, String((p - 1) * perPage));
          pageUrl = u.toString();
        } else {
          const u = new URL(listingUrl);
          u.searchParams.set("page", String(p));
          pageUrl = u.toString();
        }

        try {
          addLog(job, "status", { message: `Fetching listing page ${p}/${limitPages}... (${allCourses.length} courses so far)`, phase: "discover" });
          const pHtml = await fetchPage(pageUrl);
          const $p = cheerio.load(pHtml);

          $p("a[href]").each((_, el) => {
            const href = $p(el).attr("href") || "";
            const text = $p(el).text().trim().replace(/\s+/g, " ");
            try {
              const fullUrl = new URL(href, origin).toString();
              if (!fullUrl.startsWith(origin) || seen.has(fullUrl)) return;
              if ((isCourseUrl(fullUrl) || isCourseText(text)) && !isJunkCourseName(text)) {
                seen.add(fullUrl);
                allCourses.push({ url: fullUrl, name: text });
              }
            } catch {}
          });

          const $link = $p("a[rel='next']");
          if (!$link.length) {
            const pageLinks = $p("a[href]").filter((_, el) => /page[=\/](\d+)/i.test($p(el).attr("href") || ""));
            const pageNums = pageLinks.map((_, el) => {
              const m = ($p(el).attr("href") || "").match(/\d+/g);
              return m ? parseInt(m[m.length - 1]) : 0;
            }).get();
            const maxFoundPage = Math.max(...pageNums, 0);
            if (maxFoundPage < p) break;
          }
        } catch { break; }

        await new Promise((r) => setTimeout(r, 300));
      }
    }
  } else {
    const paginationQueue = [...nextLinks];
    const visitedPages = new Set([listingUrl]);
    addLog(job, "status", { message: `Following pagination links...`, phase: "discover" });

    while (paginationQueue.length > 0 && !job.stopped) {
      const pageUrl = paginationQueue.shift()!;
      if (visitedPages.has(pageUrl)) continue;
      visitedPages.add(pageUrl);

      try {
        const pHtml = await fetchPage(pageUrl);
        const $p = cheerio.load(pHtml);

        $p("a[href]").each((_, el) => {
          const href = $p(el).attr("href") || "";
          const text = $p(el).text().trim().replace(/\s+/g, " ");
          try {
            const fullUrl = new URL(href, origin).toString();
            if (!fullUrl.startsWith(origin) || seen.has(fullUrl)) return;
            if ((isCourseUrl(fullUrl) || isCourseText(text)) && !isJunkCourseName(text)) {
              seen.add(fullUrl);
              allCourses.push({ url: fullUrl, name: text });
            }
            if (href && $p(el).attr("rel") === "next") {
              paginationQueue.push(fullUrl);
            }
          } catch {}
        });
      } catch { break; }

      await new Promise((r) => setTimeout(r, 300));
      if (visitedPages.size > 100) break;
    }
  }

  return allCourses;
}

// Common category slug names used by course-list pages (VIT-style)
const COURSE_CATEGORY_SLUGS = [
  "bits", "mits", "mba", "bbus", "vocational", "elicos",
  "bachelor", "master", "diploma", "certificate", "graduate",
  "undergraduate", "postgraduate", "phd", "honours",
];

async function detectCourseListingPage(homeUrl: string, html: string, job: ScrapeJob): Promise<string | null> {
  const origin = new URL(homeUrl).origin;
  const $ = cheerio.load(html);

  // ── STEP 1: HEAD-probe high-priority specific paths first ────────────────────
  // These are preferred over generic "/courses" found via link scanning, because
  // sites like VIT use /course-list for their real listing while /courses just redirects.
  const highPriorityPaths = [
    "/course-list", "/course-finder", "/course-guide",
    "/study/courses", "/courses/undergraduate", "/courses/postgraduate",
  ];
  for (const path of highPriorityPaths) {
    try {
      const testUrl = `${origin}${path}`;
      const resp = await fetch(testUrl, { method: "HEAD", headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS }, signal: AbortSignal.timeout(5000) });
      if (resp.ok) {
        addLog(job, "status", { message: `Home page detected → course listing at ${testUrl} (high-priority probe)`, phase: "discover" });
        return testUrl;
      }
    } catch {}
  }

  // ── STEP 2: Link scanning — find the best-linked course listing page ─────────
  const strongUrlPatterns = [
    /\/study\/courses\b/i, /\/courses\/$/i, /\/courses\b/i,
    /\/programs\b/i, /\/programmes\b/i,
    /\/find-a-course/i, /\/search.*course/i, /\/course-search/i,
    /\/undergraduate-courses/i, /\/postgraduate-courses/i,
    /\/our-courses/i, /\/all-courses/i, /\/browse-courses/i,
    /\/course-list/i, /\/course-finder/i, /\/course-guide/i,
  ];

  const candidates: { url: string; score: number }[] = [];

  $("a[href]").each((_, el) => {
    const href = $(el).attr("href") || "";
    const text = $(el).text().trim().toLowerCase();
    try {
      const fullUrl = href.startsWith("http") ? href : new URL(href, origin).toString();
      if (!fullUrl.startsWith(origin)) return;
      const urlLower = fullUrl.toLowerCase();

      let score = 0;
      if (strongUrlPatterns.some((p) => p.test(urlLower))) score += 3;
      if (/\b(courses?|programmes?|degrees?)\b/i.test(text)) score += 2;
      if (/\b(all|search|find|browse|explore|view)\b/i.test(text)) score += 1;
      if (/\b(study|study with us|our courses)\b/i.test(text)) score += 1;

      if (score >= 3) {
        candidates.push({ url: fullUrl, score });
      }
    } catch {}
  });

  if (candidates.length > 0) {
    candidates.sort((a, b) => b.score - a.score);
    const best = candidates[0].url;
    addLog(job, "status", { message: `Home page detected → course listing found at ${best}`, phase: "discover" });
    return best;
  }

  // ── STEP 3: Broad HEAD-probe fallback ────────────────────────────────────────
  const commonCoursePaths = [
    "/courses", "/programs", "/programmes",
    "/study/programs", "/undergraduate-courses", "/postgraduate-courses",
    "/our-courses", "/find-a-course", "/course-search",
    "/study/undergraduate", "/study/postgraduate", "/academics/programs",
    "/academics/courses", "/future-students/courses", "/all-courses",
  ];

  for (const path of commonCoursePaths) {
    try {
      const testUrl = `${origin}${path}`;
      const resp = await fetch(testUrl, { method: "HEAD", headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS }, signal: AbortSignal.timeout(5000) });
      if (resp.ok) {
        addLog(job, "status", { message: `Home page detected → course listing at ${testUrl}`, phase: "discover" });
        return testUrl;
      }
    } catch {}
  }

  return null;
}

/**
 * For sites using category-filtered course list pages (e.g. VIT /course-list?course_categories[0]=bits),
 * gather links from each category variant and merge them into the main candidate list.
 */
async function expandCourseListWithCategories(listingUrl: string, existingCandidates: { url: string; name: string }[]): Promise<{ url: string; name: string }[]> {
  const origin = new URL(listingUrl).origin;
  const basePath = new URL(listingUrl).pathname;

  // Only try category expansion for short listing paths (not already filtered)
  if (!basePath.match(/\/course-list|\/course-finder|\/courses?$/i)) return existingCandidates;

  const seen = new Set(existingCandidates.map((c) => c.url));
  const extra: { url: string; name: string }[] = [];

  for (const slug of COURSE_CATEGORY_SLUGS) {
    const variants = [
      `${origin}${basePath}?course_categories[0]=${slug}`,
      `${origin}${basePath}?category=${slug}`,
      `${origin}${basePath}?type=${slug}`,
      `${origin}${basePath}/${slug}`,
    ];
    for (const variantUrl of variants) {
      try {
        const resp = await fetch(variantUrl, { method: "HEAD", headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS }, signal: AbortSignal.timeout(4000) });
        if (!resp.ok) continue;
        const html = await fetchPage(variantUrl);
        const $ = cheerio.load(html);
        $("a[href]").each((_, el) => {
          const href = $(el).attr("href") || "";
          const text = $(el).text().trim();
          try {
            const fullUrl = href.startsWith("http") ? href : new URL(href, origin).toString();
            if (!fullUrl.startsWith(origin)) return;
            if (seen.has(fullUrl)) return;
            if (!isCourseUrl(fullUrl) && !isCourseText(text)) return;
            if (isJunkCourseName(text)) return;
            seen.add(fullUrl);
            extra.push({ url: fullUrl, name: text || sitemapLocToCourseName(fullUrl) });
          } catch {}
        });
        // Only try one working variant per category
        if (extra.length > 0) break;
      } catch {}
    }
  }

  return [...existingCandidates, ...extra];
}

async function discoverUniversityPages(siteUrl: string, job: ScrapeJob): Promise<{ feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string }> {
  const result: { feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string } = {};
  const origin = new URL(siteUrl).origin;

  try {
    const homepageHtml = await fetchPage(origin);
    const $ = cheerio.load(homepageHtml);
    const visited = new Set<string>();

    $("a[href]").each((_, el) => {
      const href = $(el).attr("href") || "";
      const text = $(el).text().trim().toLowerCase();
      try {
        const rawUrl = href.startsWith("http") ? href : new URL(href, origin).toString();
        // Strip hash fragments — servers ignore them, so #FeeInformation → homepage HTML
        const fullUrl = rawUrl.split("#")[0];
        if (!fullUrl || fullUrl === origin || fullUrl === origin + "/") return;
        if (!fullUrl.startsWith(origin)) return;
        if (visited.has(fullUrl)) return;
        visited.add(fullUrl);

        const isDrupalNodeUrl = /\/node\/\d+$/.test(fullUrl);

        if (!result.feesPdf && /\.pdf/i.test(fullUrl) && /fee|tuition/i.test(fullUrl + " " + text)) {
          result.feesPdf = fullUrl;
        }
        // Strongly prefer URLs that have "tuition" explicitly in the path (not just link text)
        if (!isDrupalNodeUrl) {
          if (!result.feePage && /tuition/i.test(fullUrl) && !/fee.?help|scholarship|refund|domestic/i.test(fullUrl + " " + text)) {
            result.feePage = fullUrl;
          }
          if (!result.feePage && /\b(tuition|fee)\b/i.test(text) && /\b(international|overseas)\b/i.test(text + " " + fullUrl) && !/fee.?help|scholarship|refund|payment.?plan|domestic/i.test(fullUrl + " " + text)) {
            result.feePage = fullUrl;
          }
          if (!result.feePage && /\b(tuition.?fee|fee.?schedule|international.?fee)\b/i.test(fullUrl) && !/fee.?help|scholarship|refund/i.test(fullUrl)) {
            result.feePage = fullUrl;
          }
        }
        if (!result.requirementsPage && (/\b(entry|admission)\s*(require|criteria)/i.test(text) || /entry.?require|admission.?require/i.test(fullUrl))) {
          result.requirementsPage = fullUrl;
        }
        if (!result.entryPage && (/\b(english|language)\s*(require|proficiency|test)/i.test(text) || /english.?require|language.?require/i.test(fullUrl))) {
          result.entryPage = fullUrl;
        }
      } catch {}
    });

    const listingHtml = await fetchPage(siteUrl);
    const $listing = cheerio.load(listingHtml);
    $listing("a[href]").each((_, el) => {
      const href = $listing(el).attr("href") || "";
      const text = $listing(el).text().trim().toLowerCase();
      try {
        const rawUrl2 = href.startsWith("http") ? href : new URL(href, origin).toString();
        // Strip hash fragments — servers ignore them
        const fullUrl = rawUrl2.split("#")[0];
        if (!fullUrl || fullUrl === origin || fullUrl === origin + "/") return;
        if (!fullUrl.startsWith(origin)) return;
        const isDrupalNode = /\/node\/\d+$/.test(fullUrl);

        if (!result.feesPdf && /\.pdf/i.test(fullUrl) && /fee|tuition/i.test(fullUrl + " " + text)) {
          result.feesPdf = fullUrl;
        }
        if (!isDrupalNode) {
          if (!result.feePage && (/\b(tuition|fee)\b/i.test(text) || /tuition.?fee|fee.?schedule/i.test(fullUrl)) && !/fee.?help|scholarship|refund|domestic/i.test(fullUrl + " " + text)) {
            result.feePage = fullUrl;
          }
        }
        if (!result.requirementsPage && (/\b(entry|admission)\s*(require|criteria)/i.test(text) || /entry.?require|admission.?require/i.test(fullUrl))) {
          result.requirementsPage = fullUrl;
        }
        if (!result.entryPage && (/\b(english|language)\s*(require|proficiency|test)/i.test(text) || /english.?require|language.?require/i.test(fullUrl))) {
          result.entryPage = fullUrl;
        }
      } catch {}
    });
  } catch {}

  const commonFeePaths = [
    "/tuition-fees", "/study-with-us/tuition-fees", "/international/fees",
    "/fees", "/fees-and-scholarships", "/tuition", "/international-fees",
    "/study/fees", "/courses/fees", "/admissions/fees",
  ];
  if (!result.feePage) {
    for (const path of commonFeePaths) {
      try {
        const testUrl = `${origin}${path}`;
        const resp = await fetch(testUrl, { method: "HEAD", headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS }, signal: AbortSignal.timeout(5000) });
        if (resp.ok) {
          result.feePage = testUrl;
          break;
        }
      } catch {}
    }
  }

  if (!result.feePage || !result.requirementsPage) {
    try {
      const sitemapXml = await fetchPage(`${origin}/sitemap.xml`);
      const locs = [...sitemapXml.matchAll(/<loc>([^<]+)<\/loc>/gi)].map(m => m[1]);
      for (const loc of locs) {
        const lower = loc.toLowerCase();
        if (!result.feePage && /tuition.?fee|fee.?schedule|international.?fee/i.test(lower) && !/fee.?help|scholarship|refund/i.test(lower)) {
          result.feePage = loc;
        }
        if (!result.requirementsPage && /entry.?require|admission.?require/i.test(lower)) {
          result.requirementsPage = loc;
        }
        if (!result.entryPage && /english.?require|language.?require|english.?proficiency/i.test(lower)) {
          result.entryPage = loc;
        }
      }
    } catch {}
  }

  // Probe common university-level requirements paths (like the fee page probe above)
  if (!result.requirementsPage && !result.entryPage) {
    const commonRequirementsPaths = [
      "/minimum-entry-requirement", "/minimum-entry-requirements",
      "/entry-requirements", "/entry-requirement",
      "/international/requirements", "/international/entry-requirements",
      "/admissions/requirements", "/admissions/entry-requirements",
      "/requirements", "/apply/requirements",
      "/study/entry-requirements", "/courses/entry-requirements",
      "/international-students/requirements",
    ];
    for (const path of commonRequirementsPaths) {
      try {
        const testUrl = `${origin}${path}`;
        const resp = await fetch(testUrl, { method: "HEAD", headers: { "User-Agent": STEALTH_PROFILES[0]["User-Agent"], ...STEALTH_COMMON_HEADERS }, signal: AbortSignal.timeout(5000) });
        if (resp.ok) {
          result.requirementsPage = testUrl;
          addLog(job, "status", { message: `Found university requirements page via probe: ${testUrl}`, phase: "discover" });
          break;
        }
      } catch {}
    }
  }

  const found = Object.entries(result).filter(([_, v]) => v).map(([k, v]) => `${k}: ${v}`).join(", ");
  if (found) addLog(job, "status", { message: `Discovered university-level pages: ${found}`, phase: "discover" });

  return result;
}

interface UniversityFeeCache {
  html?: string;
  text?: string;
  fetched: boolean;
}

async function getUniversityFeePageText(feePage: string, cache: UniversityFeeCache): Promise<string> {
  if (cache.fetched) return cache.text || "";
  cache.fetched = true;
  try {
    // Strip hash fragments — servers return the same page regardless of anchor
    const cleanUrl = feePage.split("#")[0];
    if (!cleanUrl) return "";
    const html = await fetchPage(cleanUrl);
    cache.html = html;
    cache.text = cheerio.load(html)("body").text();
    return cache.text;
  } catch {
    return "";
  }
}

function getFeeTerm(context: string): string { return normalizeFeeTerm(context); }

function extractInternationalSection(text: string): string {
  // Try multiple patterns to isolate the international fee section
  const patterns = [
    /course\s*fees?\s*[-–]?\s*international[\s\S]*?(?=course\s*fees?\s*[-–]?\s*domestic|domestic\s*tuition|domestic\s*fee|$)/i,
    /international\s*(?:student\s*)?(?:tuition\s*)?fees?[\s\S]*?(?=domestic\s*(?:student\s*)?fees?|$)/i,
    /(?:fees?\s+for\s+international)[\s\S]*?(?=fees?\s+for\s+domestic|$)/i,
  ];
  for (const p of patterns) {
    const m = text.match(p);
    if (m && m[0].length > 100) return m[0];
  }
  // Fallback: find "international" block
  const idx = text.search(/\binternational\b.*\bfee\b|\bfee\b.*\binternational\b/i);
  return idx >= 0 ? text.slice(idx) : text;
}

async function extractFeeFromUniversityPage(feePage: string, courseName: string, courseData: Partial<CourseData>, cache: UniversityFeeCache, noAi = false, overrideExisting = false): Promise<void> {
  // Skip if we already have a fee — UNLESS the caller knows this page is an authoritative
  // international fee schedule and wants to override the (possibly domestic) course-page fee.
  if (courseData.internationalFee && !overrideExisting) return;

  const text = await getUniversityFeePageText(feePage, cache);
  if (!text) return;

  // Always search in the international section first, to avoid picking up domestic fees
  const intlSection = extractInternationalSection(text);
  const searchText = intlSection.length > 200 ? intlSection : text;

  // Try to find the fee by course name proximity (try progressively smaller matches)
  const nameParts = [
    courseName,  // full name
    courseName.replace(/,?\s*(major|specialisation|stream|pathway)\s+in\s+.*/i, "").trim(), // base degree name
  ];

  for (const namePart of nameParts) {
    // Escape special regex characters in course name
    const escapedName = namePart.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const CURR_PAT = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;
    const nameRegex = new RegExp(`${escapedName}[^\\n]{0,300}?(?:${CURR_PAT.source})\\s*([\\d,]+)`, "i");
    const m = searchText.match(nameRegex);
    if (m) {
      const fee = parseInt(m[1].replace(/,/g, ""));
      if (fee > 1000 && fee < 200000) {
        courseData.internationalFee = fee;
        courseData.currency = detectCurrencyFromContext(m[0]);
        courseData.feeTerm = getFeeTerm(m[0]);
        return;
      }
    }

    // Try reverse: currency then course name on nearby line
    const feeRegex = new RegExp(`${escapedName}[^\\n\\r]{0,50}\\n?[^\\n\\r]{0,50}(?:${CURR_PAT.source})([\\d,]+)`, "i");
    const m2 = searchText.match(feeRegex);
    if (m2) {
      const fee = parseInt(m2[1].replace(/,/g, ""));
      if (fee > 1000 && fee < 200000) {
        courseData.internationalFee = fee;
        courseData.currency = detectCurrencyFromContext(m2[0]);
        courseData.feeTerm = getFeeTerm(m2[0]);
        return;
      }
    }
  }

  // Word-by-word fallback (significant unique words in course name near a fee)
  const significantWords = courseName.split(/\s+/).filter(w => w.length > 4 && !/^(major|bachelor|master|graduate|diploma|certificate|engineering|studies|arts|science)$/i.test(w));
  const CURR_PAT2 = /A\$|NZ\$|CA\$|US\$|S\$|\$|£|€|AUD|NZD|CAD|USD|GBP|SGD|EUR/;
  for (const word of significantWords.slice(0, 3)) {
    const regex = new RegExp(`${word}[^\\n]{0,200}?(?:${CURR_PAT2.source})([\\d,]+)`, "i");
    const m = searchText.match(regex);
    if (m) {
      const fee = parseInt(m[1].replace(/,/g, ""));
      if (fee > 1000 && fee < 200000) {
        courseData.internationalFee = fee;
        courseData.currency = detectCurrencyFromContext(m[0]);
        courseData.feeTerm = getFeeTerm(m[0]);
        return;
      }
    }
  }

  // HTML table extraction — use the cached HTML to look for international/domestic columns
  if ((!courseData.internationalFee || overrideExisting) && cache.html) {
    try {
      const $feeHtml = cheerio.load(cache.html);
      const tableData: Partial<CourseData> = {};
      extractFeeFromHtmlTables($feeHtml, tableData);
      if (tableData.internationalFee) {
        courseData.internationalFee = tableData.internationalFee;
        if (tableData.currency) courseData.currency = tableData.currency;
        if (tableData.feeTerm) courseData.feeTerm = tableData.feeTerm;
        if (tableData.feeYear) courseData.feeYear = tableData.feeYear;
        return;
      }
    } catch {}
  }

  // Multi-amount fallback on the international section — highest = international
  if (!courseData.internationalFee || overrideExisting) {
    const allAmounts = extractAllFeeAmounts(searchText);
    if (allAmounts.length >= 1) {
      courseData.internationalFee = Math.max(...allAmounts);
      courseData.currency = detectCurrencyFromContext(searchText);
      courseData.feeTerm = normalizeFeeTerm(searchText);
      if (!courseData.feeYear) courseData.feeYear = extractFeeYear(searchText);
      return;
    }
  }

  if (!noAi && (!courseData.internationalFee || overrideExisting) && GEMINI_API_KEY) {
    try {
      const prompt = `From this university INTERNATIONAL fee schedule, find the tuition fee for the course "${courseName}".
This may show fees per trimester, semester, or year. Return ONLY the international/overseas student fee amount.
Return JSON: {"internationalFee":<number>,"currency":"<AUD|GBP|USD|EUR>","feeTerm":"<Annual|Trimester|Semester|Term|Session|Per Unit|Full Course>","feeYear":<number|null>}
Use null if not found. Important: Only return INTERNATIONAL student fees, not domestic/local fees.`;
      const trimmedText = searchText.slice(0, 6000);
      const result = await geminiChat(prompt, trimmedText, 256);
      const parsed = JSON.parse(result);
      if (parsed.internationalFee && parsed.internationalFee > 500) {
        courseData.internationalFee = parsed.internationalFee;
        courseData.currency = parsed.currency || "AUD";
        courseData.feeTerm = parsed.feeTerm || "Annual";
      }
    } catch {}
  }
}

function cheerioToCourseData(cheerioData: Partial<CourseData>, name: string, url: string): CourseData {
  return {
    courseName: cheerioData.courseName || name,
    courseWebsite: url,
    duration: cheerioData.duration,
    durationTerm: cheerioData.durationTerm,
    studyMode: cheerioData.studyMode,
    degreeLevel: cheerioData.degreeLevel,
    studyLoad: cheerioData.studyLoad,
    language: cheerioData.language || "English",
    description: cheerioData.description,
    internationalFee: cheerioData.internationalFee,
    feeTerm: cheerioData.feeTerm,
    currency: cheerioData.currency,
    ieltsOverall: cheerioData.ieltsOverall,
    ieltsListening: cheerioData.ieltsListening,
    ieltsSpeaking: cheerioData.ieltsSpeaking,
    ieltsWriting: cheerioData.ieltsWriting,
    ieltsReading: cheerioData.ieltsReading,
    pteOverall: cheerioData.pteOverall,
    pteListening: cheerioData.pteListening,
    pteSpeaking: cheerioData.pteSpeaking,
    pteWriting: cheerioData.pteWriting,
    pteReading: cheerioData.pteReading,
    toeflOverall: cheerioData.toeflOverall,
    toeflListening: cheerioData.toeflListening,
    toeflSpeaking: cheerioData.toeflSpeaking,
    toeflWriting: cheerioData.toeflWriting,
    toeflReading: cheerioData.toeflReading,
    cambridgeOverall: cheerioData.cambridgeOverall,
    duolingoOverall: cheerioData.duolingoOverall,
    intakeMonths: cheerioData.intakeMonths,
    academicLevel: cheerioData.academicLevel,
    otherRequirement: cheerioData.otherRequirement,
  };
}

function makeSemaphore(concurrency: number) {
  let running = 0;
  const queue: (() => void)[] = [];
  return async function<T>(fn: () => Promise<T>): Promise<T> {
    await new Promise<void>((resolve) => {
      if (running < concurrency) { running++; resolve(); }
      else { queue.push(resolve); }
    });
    try { return await fn(); }
    finally {
      running--;
      const next = queue.shift();
      if (next) { running++; next(); }
    }
  };
}

async function scrapeCourseBatch(
  courseLinks: { url: string; name: string }[],
  uniId: number,
  job: ScrapeJob,
  maxCourses: number,
  jobId: string,
  uniPages?: { feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string },
  universityCountry?: string,
) {
  const max = Math.min(courseLinks.length, maxCourses);
  job.totalFound = courseLinks.length;

  // Pre-fetch shared data ONCE (parallel)
  const feeCache: UniversityFeeCache = { fetched: false };
  let uniReqsText: string | null = null;
  let uniReqsHtml: string | null = null;
  // University-level English requirements (resolved ONCE, applied to every course in the batch).
  // Populated from the requirements page — first via static patterns, then AI if needed.
  let cachedEnglishReqs: Partial<CourseData> | null = null;
  if (uniPages?.requirementsPage || uniPages?.entryPage) {
    try {
      const reqUrl = uniPages.requirementsPage || uniPages.entryPage!;
      uniReqsHtml = await fetchPage(reqUrl);
      uniReqsText = cheerio.load(uniReqsHtml)("body").text();
      addLog(job, "status", { message: `Using university requirements page: ${reqUrl}`, phase: "fetch" });

      // Try static extraction from the requirements page first
      const tempReqData: Partial<CourseData> = {};
      extractEnglishFromHtml(cheerio.load(uniReqsHtml), tempReqData);
      if (!(tempReqData.ieltsOverall || tempReqData.pteOverall || tempReqData.toeflOverall)) {
        extractEnglishRequirements(uniReqsText, tempReqData);
      }

      if (tempReqData.ieltsOverall || tempReqData.pteOverall || tempReqData.toeflOverall) {
        cachedEnglishReqs = tempReqData;
        addLog(job, "status", { message: `University requirements page: IELTS=${tempReqData.ieltsOverall} PTE=${tempReqData.pteOverall} TOEFL=${tempReqData.toeflOverall}`, phase: "fetch" });
      } else if (GEMINI_API_KEY) {
        // Static extraction found nothing — requirements are likely JS-rendered.
        // Run Gemini ONCE on the requirements page and cache the result for all courses.
        try {
          addLog(job, "status", { message: "Static IELTS extraction failed — using AI on requirements page (1 call)...", phase: "fetch" });
          const compactReqs = extractCompactContent(uniReqsHtml, reqUrl);
          const enPrompt = `Extract ALL English language proficiency test requirements from this university page.
Return JSON: {"ieltsOverall":<number|null>,"ieltsReading":<number|null>,"ieltsWriting":<number|null>,"ieltsListening":<number|null>,"ieltsSpeaking":<number|null>,"pteOverall":<number|null>,"toeflOverall":<number|null>,"cambridgeOverall":<number|null>,"duolingoOverall":<number|null>}
Use null for any test not mentioned. Return ONLY valid JSON.`;
          const enResult = await geminiChat(enPrompt, compactReqs.slice(0, 10000), 200);
          const enParsed = JSON.parse(enResult);
          if (enParsed.ieltsOverall || enParsed.pteOverall || enParsed.toeflOverall) {
            cachedEnglishReqs = enParsed;
            addLog(job, "status", { message: `AI extracted university IELTS=${enParsed.ieltsOverall} PTE=${enParsed.pteOverall} TOEFL=${enParsed.toeflOverall}`, phase: "fetch" });
          }
        } catch {}
      }
    } catch {}
  }

  // Queues filled by parallel workers, flushed after all done
  const classifyQueue: { index: number; name: string; existing: Partial<CourseData>; data: CourseData }[] = [];
  const fullAIQueue: { index: number; name: string; html: string; cheerioData: ReturnType<typeof extractWithCheerio> }[] = [];
  let completed = 0;

  // Throughput tuning. HTTP fetches are cheap (~150KB+1socket each), so we run
  // many in parallel. Browser launches are heavy (~150 MB RAM per Chromium),
  // so we cap separately.
  // 30 concurrent HTTP fetches is the sweet spot: fast enough for static sites,
  // but won't overwhelm servers that start dropping connections above ~50 rps.
  const CONCURRENCY = 30;
  const BROWSER_CONCURRENCY = 8;
  const sem = makeSemaphore(CONCURRENCY);
  const browserSem = makeSemaphore(BROWSER_CONCURRENCY);
  // Courses that time out on the first pass are retried here.
  const retryQueue: { url: string; name: string; index: number }[] = [];

  // Determine once whether this batch of courses needs browser automation.
  // Fast mode disables browser entirely (5–10× faster, may miss JS-rendered fields).
  const batchNeedsBrowser = !job.fastMode && courseLinks.length > 0 && siteNeedsBrowser(courseLinks[0].url);
  if (job.fastMode) {
    addLog(job, "status", { message: "FAST MODE — browser automation disabled, using HTTP fetch only", phase: "fetch" });
  } else if (batchNeedsBrowser) {
    addLog(job, "status", { message: "JS-heavy site detected — using browser automation (International toggle + Entry Requirements tab)", phase: "fetch" });
  }

  const tasks = courseLinks.slice(0, max).map((link, i) =>
    sem(async () => {
      if (job.stopped) return;
      const num = ++completed;
      addLog(job, "progress", { current: num, total: max, courseName: link.name, message: `Fetching ${num}/${max}: ${link.name}` });

      try {
        // For JS-heavy sites use browser (clicks International toggle + Entry Requirements tab).
        // For all others fall through to the plain HTTP fetch.
        let cHtml: string;
        let browserReqsHtml: string | null = null;  // per-course requirements HTML from browser

        if (!job.fastMode && (batchNeedsBrowser || siteNeedsBrowser(link.url))) {
          const browserResult = await browserSem(() =>
            fetchPageWithBrowser(link.url, {
              clickInternational: true,
              clickRequirementsTab: true,
              expandAccordions: true,
              timeoutMs: 25_000,
            })
          );
          if (browserResult) {
            cHtml = browserResult.mainHtml;
            // Only use requirementsHtml when something extra was done
            if (browserResult.requirementsHtml !== browserResult.mainHtml) {
              browserReqsHtml = browserResult.requirementsHtml;
            }
            addLog(job, "status", { message: `[browser ✓] ${link.name} (clicks: ${browserResult.clicksPerformed.join(", ") || "none"})`, phase: "fetch" });
          } else {
            addLog(job, "status", { message: `[browser ✗ → static] ${link.name}`, phase: "fetch" });
            cHtml = await fetchPage(link.url);
          }
        } else {
          cHtml = await fetchPage(link.url);
        }

        const cheerioData = extractWithCheerio(cHtml, link.url, link.name, universityCountry);

        // Only enrich if cheerio is missing critical fields (avoids extra network round-trips for most courses)
        const needsEnrich = !cheerioData.internationalFee || !(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall);
        if (needsEnrich) {
          const relatedPages = findRelatedPages(cHtml, link.url);
          if (relatedPages.fees || relatedPages.requirements || relatedPages.entry || relatedPages.feesPdf) {
            await enrichFromRelatedPages(cheerioData, relatedPages, cHtml, link.url);
          }
        }

        // When browser gave us per-course requirements HTML (after opening the
        // Entry Requirements tab and expanding accordions), use it to extract
        // IELTS — this beats the university-level requirements page for accuracy.
        if (browserReqsHtml && !(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall)) {
          extractEnglishFromHtml(cheerio.load(browserReqsHtml), cheerioData);
          if (!(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall)) {
            extractEnglishRequirements(cheerio.load(browserReqsHtml)("body").text(), cheerioData);
          }
        }

        // If the university fee page is explicitly an international fees page, always
        // consult it even when the course page already has a fee (which may be domestic).
        const feePageIsInternational = !!uniPages?.feePage && /international/i.test(uniPages.feePage);
        if (uniPages?.feePage && (!cheerioData.internationalFee || feePageIsInternational)) {
          await extractFeeFromUniversityPage(uniPages.feePage, link.name, cheerioData, feeCache, false, feePageIsInternational);
        }
        if (!cheerioData.internationalFee && uniPages?.feesPdf) {
          try {
            const pdfData = await extractFeesFromPdf(uniPages.feesPdf, link.name);
            if (pdfData.internationalFee) {
              cheerioData.internationalFee = pdfData.internationalFee;
              cheerioData.currency = pdfData.currency || "AUD";
              cheerioData.feeTerm = pdfData.feeTerm || "Annual";
              cheerioData.feeYear = pdfData.feeYear || undefined;
            }
          } catch {}
        }

        if (uniReqsHtml && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)) {
          // Use HTML-based extraction on the university requirements page (table-aware)
          extractEnglishFromHtml(cheerio.load(uniReqsHtml), cheerioData);
        }
        if (uniReqsText && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)) {
          // Fallback: plain text
          extractEnglishRequirements(uniReqsText, cheerioData);
        }
        if (uniReqsText && !cheerioData.intakeMonths?.length) {
          extractIntakeMonths(uniReqsText, cheerioData);
        }

        // Apply the university-level cached English requirements (resolved once before this loop,
        // including any AI-extracted values when static parsing returned nothing).
        if (cachedEnglishReqs && !(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall)) {
          if (cachedEnglishReqs.ieltsOverall) { cheerioData.ieltsOverall = cachedEnglishReqs.ieltsOverall; cheerioData.ieltsReading = cachedEnglishReqs.ieltsReading || undefined; cheerioData.ieltsWriting = cachedEnglishReqs.ieltsWriting || undefined; cheerioData.ieltsListening = cachedEnglishReqs.ieltsListening || undefined; cheerioData.ieltsSpeaking = cachedEnglishReqs.ieltsSpeaking || undefined; }
          if (cachedEnglishReqs.pteOverall) cheerioData.pteOverall = cachedEnglishReqs.pteOverall;
          if (cachedEnglishReqs.toeflOverall) cheerioData.toeflOverall = cachedEnglishReqs.toeflOverall;
          if (cachedEnglishReqs.cambridgeOverall) cheerioData.cambridgeOverall = cachedEnglishReqs.cambridgeOverall;
          if ((cachedEnglishReqs as any).duolingoOverall) (cheerioData as any).duolingoOverall = (cachedEnglishReqs as any).duolingoOverall;
        }

        const hasFees = !!cheerioData.internationalFee;
        const hasEnglish = !!(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall || cheerioData.cambridgeOverall);
        const hasDuration = !!cheerioData.duration;

        if (hasFees || hasEnglish || hasDuration) {
          // Cheerio got useful data — queue for batch AI classification (cheap)
          const courseData = cheerioToCourseData(cheerioData, link.name, link.url);
          classifyQueue.push({ index: i, name: link.name, existing: courseData, data: courseData });
        } else {
          // Cheerio got nothing — queue for full AI extraction (deferred)
          fullAIQueue.push({ index: i, name: link.name, html: cHtml, cheerioData });
        }
      } catch (err) {
        const msg = (err as Error).message || "";
        const isTimeout = /timeout|aborted|abort/i.test(msg);
        if (isTimeout) {
          // Don't count as a permanent error — will retry with lower concurrency
          retryQueue.push({ url: link.url, name: link.name, index: i });
          addLog(job, "status", { message: `[timeout → will retry] ${link.name}`, phase: "fetch" });
        } else {
          job.errors++;
          addLog(job, "course", { name: link.name, status: "error", message: msg, index: i + 1 });
        }
      }
    })
  );

  // Run all parallel fetches
  await Promise.all(tasks);

  // ── Retry timed-out courses with reduced concurrency (10) ──────────────────
  if (retryQueue.length > 0 && !job.stopped) {
    addLog(job, "status", { message: `Retrying ${retryQueue.length} timed-out courses at reduced concurrency (10)...`, phase: "fetch" });
    await new Promise((r) => setTimeout(r, 2000)); // brief pause before retry
    const retrySem = makeSemaphore(10);
    let retryDone = 0;
    await Promise.all(retryQueue.map(({ url, name, index }) =>
      retrySem(async () => {
        if (job.stopped) return;
        retryDone++;
        addLog(job, "status", { message: `[retry ${retryDone}/${retryQueue.length}] ${name}`, phase: "fetch" });
        try {
          const cHtml = await fetchPage(url);
          const cheerioData = extractWithCheerio(cHtml, url, name, universityCountry);
          const needsEnrich = !cheerioData.internationalFee || !(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall);
          if (needsEnrich) {
            const relatedPages = findRelatedPages(cHtml, url);
            if (relatedPages.fees || relatedPages.requirements || relatedPages.entry || relatedPages.feesPdf) {
              await enrichFromRelatedPages(cheerioData, relatedPages, cHtml, url);
            }
          }
          if (uniPages?.feePage && !cheerioData.internationalFee) {
            const feePageIsInternational = /international/i.test(uniPages.feePage);
            await extractFeeFromUniversityPage(uniPages.feePage, name, cheerioData, feeCache, false, feePageIsInternational);
          }
          if (uniReqsHtml && !(cheerioData.ieltsOverall && cheerioData.pteOverall)) {
            extractEnglishFromHtml(cheerio.load(uniReqsHtml), cheerioData);
            if (cachedEnglishReqs) {
              if (!cheerioData.ieltsOverall && cachedEnglishReqs.ieltsOverall) cheerioData.ieltsOverall = cachedEnglishReqs.ieltsOverall;
              if (!cheerioData.pteOverall && cachedEnglishReqs.pteOverall) cheerioData.pteOverall = cachedEnglishReqs.pteOverall;
              if (!cheerioData.toeflOverall && cachedEnglishReqs.toeflOverall) cheerioData.toeflOverall = cachedEnglishReqs.toeflOverall;
            }
          }
          const courseData = cheerioToCourseData(cheerioData, name, url);
          const saved = await stageCourse(courseData, uniId, jobId, job);
          if (saved) { job.imported++; addLog(job, "course", { name, status: "staged", index: index + 1 }); }
          else { job.skipped++; addLog(job, "course", { name, status: "skipped", index: index + 1 }); }
        } catch (retryErr) {
          job.errors++;
          addLog(job, "course", { name, status: "error", message: `[retry failed] ${(retryErr as Error).message}`, index: index + 1 });
        }
      })
    ));
    addLog(job, "status", { message: `Retry complete — ${retryQueue.length - retryQueue.filter((_, ri) => ri < retryDone).length + retryDone} attempted`, phase: "fetch" });
  }

  if (job.stopped) {
    addLog(job, "status", { message: `Stopped. ${completed} fetched, processing queued data...` });
  }

  // ── Phase A: Batch-classify courses that have cheerio data (15 per AI call) ──
  if (classifyQueue.length > 0) {
    addLog(job, "status", { message: `Classifying ${classifyQueue.length} courses with AI (batched)...`, phase: "classify" });
    const CLASSIFY_BATCH = 15;
    for (let b = 0; b < classifyQueue.length; b += CLASSIFY_BATCH) {
      const batch = classifyQueue.slice(b, b + CLASSIFY_BATCH);
      const classifications = await batchClassify(batch.map((c) => ({ index: c.index, name: c.name, existing: c.existing })));
      for (const item of batch) {
        const extra = classifications.get(item.index);
        if (extra) {
          if (extra.category && !item.data.category) item.data.category = extra.category;
          if (extra.subCategory && !item.data.subCategory) item.data.subCategory = extra.subCategory;
          if (extra.degreeLevel && !item.data.degreeLevel) item.data.degreeLevel = extra.degreeLevel;
          if (extra.description && !item.data.description) item.data.description = extra.description;
        }
        const saved = await stageCourse(item.data, uniId, jobId, job);
        if (saved) { job.imported++; addLog(job, "course", { name: item.data.courseName, status: "staged", index: item.index + 1 }); }
        else { job.skipped++; addLog(job, "course", { name: item.data.courseName, status: "skipped", index: item.index + 1 }); }
      }
    }
  }

  // ── Phase B: Full AI extraction for courses where cheerio got nothing (parallel, up to 10 concurrent) ──
  if (fullAIQueue.length > 0) {
    addLog(job, "status", { message: `Running full AI extraction on ${fullAIQueue.length} courses that need it...`, phase: "extract" });
    const aiSem = makeSemaphore(10);
    await Promise.all(fullAIQueue.map((item) =>
      aiSem(async () => {
        if (job.stopped) return;
        let cData: CourseData | null = null;
        try {
          const compactContent = extractCompactContent(item.html, courseLinks[item.index].url);
          cData = await extractCourseFromPage(compactContent, item.name);
        } catch {}

        if (cData) {
          for (const [key, val] of Object.entries(item.cheerioData)) {
            if (val !== undefined && val !== null && !(cData as any)[key]) (cData as any)[key] = val;
          }
          cData.courseWebsite = cData.courseWebsite || courseLinks[item.index].url;
          const saved = await stageCourse(cData, uniId, jobId, job);
          if (saved) { job.imported++; addLog(job, "course", { name: cData.courseName, status: "staged", index: item.index + 1 }); }
          else { job.skipped++; addLog(job, "course", { name: cData.courseName, status: "skipped", index: item.index + 1 }); }
        } else if (item.cheerioData.courseName || item.name) {
          const fallbackData = cheerioToCourseData(item.cheerioData, item.name, courseLinks[item.index].url);
          const saved = await stageCourse(fallbackData, uniId, jobId, job);
          if (saved) { job.imported++; addLog(job, "course", { name: fallbackData.courseName, status: "staged (cheerio only)", index: item.index + 1 }); }
          else { job.skipped++; addLog(job, "course", { name: fallbackData.courseName, status: "skipped", index: item.index + 1 }); }
        } else {
          job.errors++;
          addLog(job, "course", { name: item.name, status: "error", message: "No extractable data", index: item.index + 1 });
        }
      })
    ));
  }
}

async function tryAlternativeUrls(url: string, job: ScrapeJob): Promise<{ html: string; resolvedUrl: string } | null> {
  const origin = new URL(url).origin;
  const pathname = new URL(url).pathname;

  const parentPath = pathname.split("/").slice(0, -1).join("/") || "/";
  const alternatives = [
    parentPath !== "/" ? `${origin}${parentPath}` : null,
    `${origin}/courses`,
    `${origin}/degrees`,
    `${origin}/programs`,
    `${origin}/study`,
    `${origin}/study/postgraduate`,
    `${origin}/study/undergraduate`,
    `${origin}/study/international`,
    `${origin}/study-with-us`,
    `${origin}/academics`,
    origin,
  ].filter((u): u is string => u !== null && u !== url);

  for (const altUrl of alternatives) {
    try {
      addLog(job, "status", { message: `Trying alternative URL: ${altUrl}`, phase: "fetch" });
      const html = await fetchPage(altUrl);
      return { html, resolvedUrl: altUrl };
    } catch {}
  }
  return null;
}

async function runScrapeJob(job: ScrapeJob, url: string, uniId: number, jobId: string, universityCountry?: string, manualPages?: { feePage?: string; requirementsPage?: string; entryPage?: string }) {
  try {
    if (!universityCountry) {
      try {
        const uniRows = await db.select({ country: universitiesTable.country }).from(universitiesTable).where(eq(universitiesTable.id, uniId));
        if (uniRows[0]?.country && uniRows[0].country !== "Unknown") universityCountry = uniRows[0].country;
      } catch {}
    }
    addLog(job, "status", { message: `Fetching ${url}...`, phase: "fetch" });
    const origin = new URL(url).origin;

    let html: string | null = null;
    let resolvedUrl = url;
    try {
      html = await fetchPage(url);
    } catch (err) {
      addLog(job, "status", { message: `URL returned error: ${(err as Error).message}. Searching for alternative pages...`, phase: "fetch" });
      const alt = await tryAlternativeUrls(url, job);
      if (alt) {
        html = alt.html;
        resolvedUrl = alt.resolvedUrl;
        addLog(job, "status", { message: `Found working page at ${resolvedUrl}`, phase: "fetch" });
      }
    }

    const urlPath = new URL(resolvedUrl).pathname;
    const isHomePage = !urlPath || urlPath === "/" || urlPath === "/index.html";
    if (isHomePage && html) {
      addLog(job, "status", { message: "Home page detected. Searching for course listing page...", phase: "discover" });
      const courseListingUrl = await detectCourseListingPage(resolvedUrl, html, job);
      if (courseListingUrl) {
        try {
          const listingHtml = await fetchPage(courseListingUrl);
          html = listingHtml;
          resolvedUrl = courseListingUrl;
          addLog(job, "status", { message: `Switched to course listing: ${courseListingUrl}`, phase: "fetch" });
        } catch {}
      }
    }

    addLog(job, "status", { message: "Discovering university-level fee & requirements pages...", phase: "discover" });
    const uniPages = await discoverUniversityPages(resolvedUrl, job);
    // Apply manually-provided pages — they override auto-discovered ones
    if (manualPages?.feePage) { uniPages.feePage = manualPages.feePage; addLog(job, "status", { message: `Using provided fee page: ${manualPages.feePage}`, phase: "discover" }); }
    if (manualPages?.requirementsPage) { uniPages.requirementsPage = manualPages.requirementsPage; addLog(job, "status", { message: `Using provided requirements page: ${manualPages.requirementsPage}`, phase: "discover" }); }
    if (manualPages?.entryPage) { uniPages.entryPage = manualPages.entryPage; }

    if (!html) {
      addLog(job, "status", { message: "No direct page available. Scanning sitemap for course URLs...", phase: "discover" });
      const sitemapCourses = await discoverCourseLinksFromSitemap(origin, job);
      if (sitemapCourses.length > 0) {
        addLog(job, "status", { message: `Found ${sitemapCourses.length} courses from sitemap. Extracting...`, phase: "extract", totalCourses: sitemapCourses.length });
        await scrapeCourseBatch(sitemapCourses, uniId, job, sitemapCourses.length, jobId, uniPages, universityCountry);
        addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }

      addLog(job, "status", { message: "Crawling site for course pages...", phase: "discover" });
      const crawled = await crawlForCourseLinks(origin, origin, job, 2);
      if (crawled.length > 0) {
        addLog(job, "status", { message: `Found ${crawled.length} courses by crawling. Extracting...`, phase: "extract", totalCourses: crawled.length });
        await scrapeCourseBatch(crawled, uniId, job, crawled.length, jobId, uniPages, universityCountry);
        addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }

      addLog(job, "error", { message: "Could not reach this URL or find any course pages on this site." });
      job.status = "failed";
      job.completedAt = Date.now();
      return;
    }

    // Fast rule-based page classifier — no AI, no network cost.
    // AI analyzePage is preserved below as a fallback only when rules say "unknown" AND sitemap is empty.
    const rulesResult = classifyPageByRules(html, resolvedUrl);
    addLog(job, "status", {
      message: `Page type: ${rulesResult.pageType} — ${rulesResult.reason}`,
      phase: "analyze",
    });
    let analysis: { pageType: string; courseLinks?: { url: string; name: string }[] } = rulesResult;

    if (analysis.pageType === "detail") {
      addLog(job, "status", { message: "Found single course page. Extracting...", phase: "extract" });
      const cheerioData = extractWithCheerio(html, resolvedUrl, "");

      const relatedPages = findRelatedPages(html, resolvedUrl);
      if (relatedPages.fees || relatedPages.requirements || relatedPages.entry || relatedPages.feesPdf) {
        addLog(job, "status", { message: "Checking related pages/PDFs for fees/requirements...", phase: "enrich" });
        await enrichFromRelatedPages(cheerioData, relatedPages, html, resolvedUrl);
      } else if (!(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall) || !cheerioData.internationalFee) {
        await enrichFromRelatedPages(cheerioData, relatedPages, html, resolvedUrl);
      }

      if (uniPages.feePage) {
        addLog(job, "status", { message: "Checking university fee page...", phase: "enrich" });
        const singleFeeCache: UniversityFeeCache = { fetched: false };
        const singleFeePageIsIntl = /international/i.test(uniPages.feePage);
        if (!cheerioData.internationalFee || singleFeePageIsIntl) {
          await extractFeeFromUniversityPage(uniPages.feePage, cheerioData.courseName || "", cheerioData, singleFeeCache, false, singleFeePageIsIntl);
        }
      }
      if (!cheerioData.internationalFee && uniPages.feesPdf) {
        try {
          const pdfData = await extractFeesFromPdf(uniPages.feesPdf, cheerioData.courseName || "");
          if (pdfData.internationalFee) {
            cheerioData.internationalFee = pdfData.internationalFee;
            cheerioData.currency = pdfData.currency || "AUD";
            cheerioData.feeTerm = pdfData.feeTerm || "Annual";
          }
        } catch {}
      }

      const compactContent = extractCompactContent(html, resolvedUrl);
      const aiData = await extractCourseFromPage(compactContent, cheerioData.courseName || "course");

      if (aiData) {
        // Merge cheerioData into aiData — Cheerio wins for any field it filled
        for (const [key, val] of Object.entries(cheerioData)) {
          if (val !== undefined && val !== null && !(aiData as any)[key]) {
            (aiData as any)[key] = val;
          }
        }
        aiData.courseWebsite = aiData.courseWebsite || resolvedUrl;
        const saved = await stageCourse(aiData, uniId, jobId, job);
        job.totalFound = 1;
        if (saved) job.imported = 1; else job.skipped = 1;
        addLog(job, "course", { name: aiData.courseName, status: saved ? "staged" : "skipped (duplicate)" });
        addLog(job, "done", { totalFound: 1, imported: job.imported, skipped: job.skipped, errors: 0 });
      } else if (cheerioData.courseName) {
        // AI failed but Cheerio extracted data — use it directly rather than losing the course
        addLog(job, "status", { message: "AI extraction failed; saving Cheerio-extracted data as fallback.", phase: "extract" });
        cheerioData.courseWebsite = cheerioData.courseWebsite || resolvedUrl;
        const saved = await stageCourse(cheerioData as CourseData, uniId, jobId, job);
        job.totalFound = 1;
        if (saved) job.imported = 1; else job.skipped = 1;
        addLog(job, "course", { name: cheerioData.courseName, status: saved ? "staged (partial)" : "skipped (duplicate)" });
        addLog(job, "done", { totalFound: 1, imported: job.imported, skipped: job.skipped, errors: 0 });
      } else {
        addLog(job, "error", { message: "Could not extract course data from this page." });
      }
      job.status = "completed";
      job.completedAt = Date.now();
      return;
    }

    addLog(job, "status", { message: "Phase 1: Discovering candidate course URLs from all sources...", phase: "discover" });

    let rawCandidates: { url: string; name: string }[] = [];

    // --- Source A: Sitemap (most comprehensive for large universities) ---
    const sitemapCandidates = await discoverCourseLinksFromSitemap(origin, job);

    // AI fallback: only when rules returned "unknown" AND sitemap is empty.
    // This is rare (JS-rendered listing pages with no static links and no sitemap).
    if (analysis.pageType === "unknown" && sitemapCandidates.length === 0) {
      addLog(job, "status", { message: "Rules uncertain + no sitemap — trying AI page analysis (1 call)...", phase: "analyze" });
      try {
        const pageContent = extractFullPageContent(html, resolvedUrl);
        const aiResult = await analyzePage(pageContent);
        if (aiResult.pageType !== "unknown") {
          analysis = aiResult;
          addLog(job, "status", { message: `AI classified page as: ${aiResult.pageType}`, phase: "analyze" });
        }
      } catch (aiErr) {
        addLog(job, "status", { message: `AI fallback failed (${(aiErr as Error).message}) — continuing with HTML links`, phase: "analyze" });
      }
    }

    // --- Source B: Listing page HTML + hidden API (fallback or supplement) ---
    if (analysis.pageType === "unknown") {
      const apiCourses = await tryDiscoverApiEndpoints(html, resolvedUrl, job);
      if (apiCourses && apiCourses.length > 0) {
        addLog(job, "status", {
          message: `Found ${apiCourses.length} courses via API endpoint. Validating...`,
          phase: "extract",
          totalCourses: apiCourses.length,
        });
        await scrapeCourseBatch(apiCourses, uniId, job, apiCourses.length, jobId, uniPages, universityCountry);
        addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
        if (job.imported > 0) {
          const config: ScrapeConfig = { courseLinks: apiCourses, uniPages, resolvedUrl, lastScrapedAt: new Date().toISOString() };
          job.discoveredConfig = config;
          await db.update(universitiesTable).set({ scrapeConfig: config }).where(eq(universitiesTable.id, uniId));
        }
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }
    }

    if (sitemapCandidates.length >= 10) {
      // Sitemap is the best source — use it exclusively to avoid listing page navigation pollution
      addLog(job, "status", {
        message: `Sitemap found ${sitemapCandidates.length} candidate URLs. Analyzing to identify real course pages...`,
        phase: "discover",
      });
      rawCandidates = sitemapCandidates;
    } else {
      // Fallback: extract links from listing page HTML (AI-identified + HTML scraping)
      let listingLinks: { url: string; name: string }[] = [];
      if (analysis.pageType === "listing" && analysis.courseLinks?.length) {
        listingLinks = analysis.courseLinks.filter((l) => l.url && l.name && !isJunkCourseName(l.name));
      }
      // Only add HTML-parsed links if sitemap gave very few results
      const $ = cheerio.load(html);
      $("a[href]").each((_, el) => {
        const href = $(el).attr("href") || "";
        const text = $(el).text().trim().replace(/\s+/g, " ");
        try {
          const fullUrl = new URL(href, origin).toString();
          if (!fullUrl.startsWith(origin)) return;
          if ((isCourseUrl(fullUrl) || isCourseText(text)) && !isJunkCourseName(text)) {
            if (!listingLinks.find((l) => l.url === fullUrl)) {
              listingLinks.push({ url: fullUrl, name: text });
            }
          }
        } catch {}
      });

      // Follow pagination if the listing page has multiple pages
      if (listingLinks.length > 0) {
        const listingBodyText = $.root().text();
        const hasPagination = /showing\s+[\d,]+\s*[-–]\s*[\d,]+\s+of\s+[\d,]+/i.test(listingBodyText) ||
          $("a[rel='next'], [class*='pagination'] a, [class*='pager'] a, [aria-label*='next'], [aria-label*='Next']").length > 0;
        if (hasPagination) {
          addLog(job, "status", { message: `Listing page is paginated — following all pages for complete course list...`, phase: "discover" });
          listingLinks = await followPaginatedListing(resolvedUrl, html, job, listingLinks);
        }
      }

      rawCandidates = listingLinks;
      // Supplement with sitemap if available
      if (sitemapCandidates.length > 0) {
        const seen = new Set(rawCandidates.map((l) => l.url));
        for (const c of sitemapCandidates) {
          if (!seen.has(c.url)) { seen.add(c.url); rawCandidates.push(c); }
        }
      }
    }

    // --- Phase 2: Research & Validate — do NOT fetch everything blindly ---
    // Sample pages to confirm which candidates are genuine course pages
    let courseLinks: { url: string; name: string }[] = [];
    let researchStats = { validSamples: 0, rejectedSamples: 0, validExamples: [] as string[], rejectedExamples: [] as string[] };
    if (rawCandidates.length > 0) {
      addLog(job, "status", {
        message: `Phase 2: Researching ${rawCandidates.length} candidates — comparing pages to find genuine course pages before fetching...`,
        phase: "discover",
      });
      const result = await researchAndValidateCourseLinks(rawCandidates, job);
      courseLinks = result.links;
      researchStats = { validSamples: result.validSamples, rejectedSamples: result.rejectedSamples, validExamples: result.validExamples, rejectedExamples: result.rejectedExamples };

      // For category-filtered listing pages (e.g. VIT /course-list?course_categories[0]=bits),
      // probe each known category slug to discover courses that only appear under specific filters
      if (/\/course-list|\/course-finder|\/courses?\/?$/i.test(new URL(resolvedUrl).pathname)) {
        const before = courseLinks.length;
        courseLinks = await expandCourseListWithCategories(resolvedUrl, courseLinks);
        const added = courseLinks.length - before;
        if (added > 0) {
          addLog(job, "status", { message: `Category expansion found ${added} additional course links (total: ${courseLinks.length})`, phase: "discover" });
        }
      }
    }

    if (courseLinks.length > 0) {
      // --- Approval Gate: auto-proceed when confidence is high, else ask user ---
      const sampleTotal = researchStats.validSamples + researchStats.rejectedSamples;
      const confidenceRatio = sampleTotal > 0 ? researchStats.validSamples / sampleTotal : 0;
      // High confidence: >= 75% of sampled pages confirmed + at least 2 valid samples
      const highConfidence = researchStats.validSamples >= 2 && (researchStats.rejectedSamples === 0 || confidenceRatio >= 0.75);
      const estMinutes = Math.max(1, Math.ceil(courseLinks.length / 25 * 4 / 60));
      const approvalSummary: ApprovalSummary = {
        totalCourses: courseLinks.length,
        validSamples: researchStats.validSamples,
        rejectedSamples: researchStats.rejectedSamples,
        sampleTotal,
        validExamples: researchStats.validExamples,
        rejectedExamples: researchStats.rejectedExamples,
        estimatedMinutes: estMinutes,
      };

      if (highConfidence) {
        addLog(job, "status", {
          message: `High confidence: ${researchStats.validSamples}/${sampleTotal} samples valid (${Math.round(confidenceRatio * 100)}%). Auto-proceeding with ${courseLinks.length} courses (~${estMinutes} min).`,
          phase: "discover",
          totalCourses: courseLinks.length,
        });
        // Notify the UI about what was found (informational, not blocking)
        job.approvalSummary = approvalSummary;
      } else {
        // Low confidence — ask user before committing
        const proceed = await waitForApproval(job, approvalSummary);
        if (!proceed || job.stopped) {
          addLog(job, "status", { message: "Bulk fetch cancelled by user.", phase: "done" });
          job.status = "stopped";
          job.completedAt = Date.now();
          return;
        }
      }

      addLog(job, "status", {
        message: `Phase 3: Fetching ${courseLinks.length} validated course pages...`,
        phase: "extract",
        totalCourses: courseLinks.length,
      });
      await scrapeCourseBatch(courseLinks, uniId, job, courseLinks.length, jobId, uniPages, universityCountry);
      addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });

      if (job.imported > 0) {
        const config: ScrapeConfig = {
          courseLinks,
          uniPages,
          resolvedUrl,
          lastScrapedAt: new Date().toISOString(),
        };
        job.discoveredConfig = config;
        await db.update(universitiesTable).set({ scrapeConfig: config }).where(eq(universitiesTable.id, uniId));
        addLog(job, "status", { message: `Saved scraping config (${courseLinks.length} links) for future no-AI re-scrapes` });
      }

      job.status = "completed";
      job.completedAt = Date.now();
      return;
    }

    addLog(job, "error", { message: "Could not find any course links on this page. Try pasting a direct course listing or course page URL." });
    job.status = "failed";
    job.completedAt = Date.now();
  } catch (err) {
    addLog(job, "error", { message: `Scraping failed: ${(err as Error).message}` });
    job.status = "failed";
    job.completedAt = Date.now();
  }
}

router.post("/scrape/start", async (req: Request, res: Response): Promise<void> => {
  const { url, universityId, universityName, universityCountry, universityCity, feePage, requirementsPage, fastMode } = req.body as {
    url: string;
    universityId?: number;
    universityName?: string;
    universityCountry?: string;
    universityCity?: string;
    feePage?: string;
    requirementsPage?: string;
    fastMode?: boolean;
  };

  if (!url) { res.status(400).json({ error: "URL is required" }); return; }
  if (!GEMINI_API_KEY) { res.status(500).json({ error: "GEMINI_API_KEY not configured" }); return; }

  try {
    let uniId: number;
    let uniName = universityName || "";
    if (universityId) {
      const u = await db.select().from(universitiesTable).where(eq(universitiesTable.id, universityId));
      if (!u[0]) { res.status(404).json({ error: "University not found" }); return; }
      uniId = u[0].id;
      uniName = u[0].name;
    } else if (universityName) {
      const existing = await db.select().from(universitiesTable).where(eq(universitiesTable.name, universityName));
      if (existing[0]) {
        uniId = existing[0].id;
      } else {
        const [created] = await db.insert(universitiesTable).values({
          name: universityName,
          country: universityCountry || "Unknown",
          city: universityCity || "Unknown",
        }).returning();
        uniId = created.id;
      }
    } else {
      res.status(400).json({ error: "University ID or name is required" });
      return;
    }

    const jobId = `scrape_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const job: ScrapeJob = {
      id: jobId,
      status: "running",
      logs: [],
      imported: 0,
      skipped: 0,
      errors: 0,
      totalFound: 0,
      current: 0,
      startedAt: Date.now(),
    };

    job.universityId = uniId;
    job.universityName = uniName;
    job.url = url;
    job.fastMode = !!fastMode;
    scrapeJobs.set(jobId, job);
    addLog(job, "status", { message: `Using university: ${uniName} (ID: ${uniId})${fastMode ? " — FAST MODE (browser disabled)" : ""}` });

    await db.update(universitiesTable).set({ scrapeUrl: url }).where(eq(universitiesTable.id, uniId));

    const manualPages = (feePage || requirementsPage) ? { feePage: feePage || undefined, requirementsPage: requirementsPage || undefined } : undefined;
    runScrapeJob(job, url, uniId, jobId, universityCountry, manualPages).catch((err) => {
      addLog(job, "error", { message: `Fatal error: ${(err as Error).message}` });
      job.status = "failed";
      job.completedAt = Date.now();
    });

    res.json({ jobId, message: "Scraping started in background" });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

async function runNoAiScrapeJob(job: ScrapeJob, config: ScrapeConfig, uniId: number, jobId: string) {
  try {
    addLog(job, "status", { message: `Re-scraping with saved config (${config.courseLinks.length} course links, no AI)...`, phase: "fetch" });

    const uniPages = config.uniPages;
    const found = Object.entries(uniPages).filter(([_, v]) => v).map(([k, v]) => `${k}: ${v}`).join(", ");
    if (found) addLog(job, "status", { message: `Using saved university pages: ${found}`, phase: "discover" });

    const feeCache: UniversityFeeCache = { fetched: false };
    let uniReqsText: string | null = null;
    let uniReqsHtml: string | null = null;

    if (uniPages?.requirementsPage || uniPages?.entryPage) {
      try {
        const reqUrl = uniPages.requirementsPage || uniPages.entryPage!;
        uniReqsHtml = await fetchPage(reqUrl);
        uniReqsText = cheerio.load(uniReqsHtml)("body").text();
        addLog(job, "status", { message: `Using university requirements page: ${reqUrl}`, phase: "fetch" });
      } catch {}
    }

    const max = config.courseLinks.length;
    job.totalFound = max;
    const stagedCourses: { index: number; data: CourseData }[] = [];
    let completed = 0;

    const CONCURRENCY = 25;
    const sem = makeSemaphore(CONCURRENCY);

    await Promise.all(config.courseLinks.slice(0, max).map((link, i) =>
      sem(async () => {
        if (job.stopped) return;
        const num = ++completed;
        addLog(job, "progress", { current: num, total: max, courseName: link.name, message: `Fetching ${num}/${max}: ${link.name}` });

        try {
          const cHtml = await fetchPage(link.url);
          const cheerioData = extractWithCheerio(cHtml, link.url, link.name);

          const needsEnrich = !cheerioData.internationalFee || !(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall);
          if (needsEnrich) {
            const relatedPages = findRelatedPages(cHtml, link.url);
            if (relatedPages.fees || relatedPages.requirements || relatedPages.entry) {
              await Promise.all([
                relatedPages.fees && fetchPage(relatedPages.fees).then(h => {
                  const t = cheerio.load(h)("body").text();
                  if (!cheerioData.internationalFee) extractInternationalFees(t, cheerioData);
                  if (!cheerioData.intakeMonths?.length) extractIntakeMonths(t, cheerioData);
                }).catch(() => {}),
                (relatedPages.entry || relatedPages.requirements) && fetchPage((relatedPages.entry || relatedPages.requirements)!).then(h => {
                  const t = cheerio.load(h)("body").text();
                  extractEnglishRequirements(t, cheerioData);
                  if (!cheerioData.internationalFee) extractInternationalFees(t, cheerioData);
                }).catch(() => {}),
              ].filter(Boolean));
            }
          }

          const feePageIsIntl = !!uniPages?.feePage && /international/i.test(uniPages.feePage);
          if (uniPages?.feePage && (!cheerioData.internationalFee || feePageIsIntl)) {
            await extractFeeFromUniversityPage(uniPages.feePage, link.name, cheerioData, feeCache, true, feePageIsIntl);
          }
          if (uniReqsHtml && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)) {
            extractEnglishFromHtml(cheerio.load(uniReqsHtml), cheerioData);
          }
          if (uniReqsText && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)) {
            extractEnglishRequirements(uniReqsText, cheerioData);
          }
          if (uniReqsText && !cheerioData.intakeMonths?.length) {
            extractIntakeMonths(uniReqsText, cheerioData);
          }

          stagedCourses.push({ index: i, data: cheerioToCourseData(cheerioData, link.name, link.url) });
        } catch (err) {
          job.errors++;
          addLog(job, "course", { name: link.name, status: "error", message: (err as Error).message, index: i + 1 });
        }
      })
    ));

    // Stage all collected courses
    addLog(job, "status", { message: `Staging ${stagedCourses.length} courses...`, phase: "stage" });
    for (const item of stagedCourses.sort((a, b) => a.index - b.index)) {
      const saved = await stageCourse(item.data, uniId, jobId, job);
      if (saved) { job.imported++; addLog(job, "course", { name: item.data.courseName, status: "staged", index: item.index + 1 }); }
      else { job.skipped++; addLog(job, "course", { name: item.data.courseName, status: "skipped", index: item.index + 1 }); }
    }

    addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
    if (job.status !== "stopped") {
      job.status = "completed";
    }
    job.completedAt = Date.now();
  } catch (err) {
    addLog(job, "error", { message: `Re-scraping failed: ${(err as Error).message}` });
    job.status = "failed";
    job.completedAt = Date.now();
  }
}

router.post("/scrape/rescrape", async (req: Request, res: Response): Promise<void> => {
  const { universityId } = req.body as { universityId: number };
  if (!universityId) { res.status(400).json({ error: "University ID is required" }); return; }

  try {
    const [uni] = await db.select().from(universitiesTable).where(eq(universitiesTable.id, universityId));
    if (!uni) { res.status(404).json({ error: "University not found" }); return; }
    if (!uni.scrapeConfig) { res.status(400).json({ error: "No saved scraping config for this university. Run a full AI scrape first." }); return; }

    const config = uni.scrapeConfig as ScrapeConfig;

    const jobId = `scrape_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const job: ScrapeJob = {
      id: jobId,
      status: "running",
      logs: [],
      imported: 0,
      skipped: 0,
      errors: 0,
      totalFound: 0,
      current: 0,
      startedAt: Date.now(),
    };

    job.universityId = uni.id;
    job.universityName = uni.name;
    job.url = uni.scrapeUrl || config.resolvedUrl;
    scrapeJobs.set(jobId, job);
    addLog(job, "status", { message: `Re-scraping ${uni.name} using saved config (NO AI, zero cost)` });

    runNoAiScrapeJob(job, config, uni.id, jobId).catch((err) => {
      addLog(job, "error", { message: `Fatal error: ${(err as Error).message}` });
      job.status = "failed";
      job.completedAt = Date.now();
    });

    res.json({ jobId, message: "Re-scraping started (no AI)" });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.get("/scrape/status/:jobId", (req: Request, res: Response): void => {
  const job = scrapeJobs.get(req.params.jobId);
  if (!job) { res.status(404).json({ error: "Job not found" }); return; }

  const sinceIndex = parseInt(req.query.since as string) || 0;
  const newLogs = job.logs.slice(sinceIndex);

  res.json({
    id: job.id,
    status: job.status,
    imported: job.imported,
    skipped: job.skipped,
    errors: job.errors,
    totalFound: job.totalFound,
    current: job.current,
    startedAt: job.startedAt,
    completedAt: job.completedAt,
    universityId: job.universityId,
    universityName: job.universityName,
    url: job.url,
    logs: newLogs,
    logIndex: job.logs.length,
    awaitingApproval: job.awaitingApproval ? job.awaitingApproval.summary : undefined,
  });
});

router.post("/scrape/stop/:jobId", (req: Request, res: Response): void => {
  const job = scrapeJobs.get(req.params.jobId);
  if (!job) { res.status(404).json({ error: "Job not found" }); return; }
  if (job.status !== "running" && job.status !== "awaiting_approval") { res.status(400).json({ error: "Job is not running" }); return; }

  // If job is waiting for approval, resolve the promise with false (cancel)
  if (job.awaitingApproval) {
    job.awaitingApproval.resolve(false);
    job.awaitingApproval = undefined;
  }

  job.stopped = true;
  job.status = "stopped";
  job.completedAt = Date.now();
  addLog(job, "status", { message: "Scraping stopped by user" });
  addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });

  res.json({ message: "Scraping stopped", imported: job.imported });
});

router.post("/scrape/approve/:jobId", (req: Request, res: Response): void => {
  const job = scrapeJobs.get(req.params.jobId);
  if (!job) { res.status(404).json({ error: "Job not found" }); return; }
  if (!job.awaitingApproval) { res.status(400).json({ error: "Job is not awaiting approval" }); return; }

  const { proceed } = req.body as { proceed: boolean };
  job.awaitingApproval.resolve(!!proceed);
  job.awaitingApproval = undefined;
  job.status = "running";
  addLog(job, "status", {
    message: proceed ? "User confirmed — starting bulk course fetch..." : "User cancelled bulk fetch.",
    phase: proceed ? "extract" : "done",
  });

  res.json({ ok: true, proceed: !!proceed });
});

router.get("/scrape/jobs", (_req: Request, res: Response): void => {
  const jobs = Array.from(scrapeJobs.values())
    .sort((a, b) => b.startedAt - a.startedAt)
    .slice(0, 20)
    .map(j => ({
      id: j.id,
      status: j.status,
      imported: j.imported,
      skipped: j.skipped,
      errors: j.errors,
      totalFound: j.totalFound,
      current: j.current,
      startedAt: j.startedAt,
      completedAt: j.completedAt,
    }));
  res.json(jobs);
});

router.get("/scrape/staged/:jobId", async (req: Request, res: Response): Promise<void> => {
  try {
    const { jobId } = req.params;
    const courses = await db.select().from(scrapedCoursesTable)
      .where(eq(scrapedCoursesTable.scrapeJobId, jobId));
    res.json(courses);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.get("/scrape/staged", async (_req: Request, res: Response): Promise<void> => {
  try {
    const result = await pool.query(`
      SELECT sc.*, u.name as university_name 
      FROM scraped_courses sc 
      JOIN universities u ON sc.university_id = u.id 
      WHERE sc.status = 'pending' 
      ORDER BY sc.created_at DESC
    `);
    res.json(result.rows);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.put("/scrape/staged/:id", async (req: Request, res: Response): Promise<void> => {
  try {
    const id = parseInt(req.params.id);
    const body = req.body;
    const allowedFields = [
      "courseName", "category", "subCategory", "courseWebsite", "duration", "durationTerm",
      "studyMode", "degreeLevel", "studyLoad", "language", "description", "otherRequirement",
      "internationalFee", "feeTerm", "feeYear", "currency",
      "ieltsOverall", "ieltsListening", "ieltsSpeaking", "ieltsWriting", "ieltsReading",
      "pteOverall", "pteListening", "pteSpeaking", "pteWriting", "pteReading",
      "toeflOverall", "toeflListening", "toeflSpeaking", "toeflWriting", "toeflReading",
      "cambridgeOverall", "duolingoOverall", "intakeMonths",
      "academicLevel", "academicScore", "scoreType", "academicCountry", "scholarship",
    ] as const;
    const updates: Record<string, unknown> = {};
    for (const key of allowedFields) {
      if (key in body) updates[key] = body[key];
    }

    const [existing] = await db.select().from(scrapedCoursesTable).where(eq(scrapedCoursesTable.id, id));
    if (!existing || existing.status !== "pending") {
      res.status(400).json({ error: "Can only edit pending courses" });
      return;
    }

    await db.update(scrapedCoursesTable)
      .set(updates)
      .where(eq(scrapedCoursesTable.id, id));

    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.delete("/scrape/staged/:id", async (req: Request, res: Response): Promise<void> => {
  try {
    const id = parseInt(req.params.id);
    await db.delete(scrapedCoursesTable).where(eq(scrapedCoursesTable.id, id));
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

async function approveSingleCourse(course: typeof scrapedCoursesTable.$inferSelect): Promise<{ success: boolean; courseId?: number; error?: string }> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");

    const dup = await client.query(
      "SELECT id FROM courses WHERE university_id=$1 AND name=$2 LIMIT 1",
      [course.universityId, course.courseName],
    );

    let courseId: number;
    if (dup.rows.length > 0) {
      courseId = dup.rows[0].id;
      await client.query(
        `UPDATE courses SET category=$2, sub_category=$3, course_website=$4, duration=$5, duration_term=$6, 
         study_mode=$7, degree_level=$8, study_load=$9, language=$10, description=$11, other_requirement=$12, updated_at=NOW()
         WHERE id=$1`,
        [courseId, course.category, course.subCategory, course.courseWebsite, course.duration, course.durationTerm,
         course.studyMode, course.degreeLevel, course.studyLoad, course.language, course.description, course.otherRequirement],
      );
      await client.query("DELETE FROM fees WHERE course_id=$1", [courseId]);
      await client.query("DELETE FROM english_requirements WHERE course_id=$1", [courseId]);
      await client.query("DELETE FROM intakes WHERE course_id=$1", [courseId]);
      await client.query("DELETE FROM academic_requirements WHERE course_id=$1", [courseId]);
      await client.query("DELETE FROM scholarships WHERE course_id=$1", [courseId]);
    } else {
      const cRes = await client.query(
        `INSERT INTO courses (university_id, name, category, sub_category, course_website, duration, duration_term, 
         study_mode, degree_level, study_load, language, description, other_requirement, status)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'active') RETURNING id`,
        [course.universityId, course.courseName, course.category, course.subCategory, course.courseWebsite,
         course.duration, course.durationTerm, course.studyMode, course.degreeLevel, course.studyLoad,
         course.language, course.description, course.otherRequirement],
      );
      courseId = cRes.rows[0].id;
    }

    if (course.intakeMonths && Array.isArray(course.intakeMonths) && course.intakeMonths.length > 0) {
      for (const m of course.intakeMonths) {
        await client.query("INSERT INTO intakes (course_id, intake_month) VALUES ($1,$2)", [courseId, m]);
      }
    }

    if (course.internationalFee) {
      await client.query(
        "INSERT INTO fees (course_id, international_fee, fee_term, fee_year, currency) VALUES ($1,$2,$3,$4,$5)",
        [courseId, course.internationalFee, course.feeTerm, course.feeYear, course.currency],
      );
    }

    if (course.ieltsOverall) {
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, listening, speaking, writing, reading, overall) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        [courseId, "IELTS", course.ieltsListening, course.ieltsSpeaking, course.ieltsWriting, course.ieltsReading, course.ieltsOverall],
      );
    }
    if (course.pteOverall) {
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, listening, speaking, writing, reading, overall) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        [courseId, "PTE", course.pteListening, course.pteSpeaking, course.pteWriting, course.pteReading, course.pteOverall],
      );
    }
    if (course.toeflOverall) {
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, listening, speaking, writing, reading, overall) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        [courseId, "TOEFL", course.toeflListening, course.toeflSpeaking, course.toeflWriting, course.toeflReading, course.toeflOverall],
      );
    }
    if (course.cambridgeOverall) {
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, overall) VALUES ($1,$2,$3)",
        [courseId, "Cambridge CAE", course.cambridgeOverall],
      );
    }
    if (course.duolingoOverall) {
      await client.query(
        "INSERT INTO english_requirements (course_id, test_type, overall) VALUES ($1,$2,$3)",
        [courseId, "Duolingo", course.duolingoOverall],
      );
    }

    if (course.academicLevel || course.academicScore) {
      await client.query(
        "INSERT INTO academic_requirements (course_id, academic_level, academic_score, score_type, academic_country) VALUES ($1,$2,$3,$4,$5)",
        [courseId, course.academicLevel, course.academicScore, course.scoreType, course.academicCountry],
      );
    }

    if (course.scholarship) {
      await client.query("INSERT INTO scholarships (course_id, name, details) VALUES ($1,$2,$3)", [courseId, "Scholarship", course.scholarship]);
    }

    await client.query("UPDATE scraped_courses SET status='approved' WHERE id=$1", [course.id]);
    await client.query("COMMIT");
    return { success: true, courseId };
  } catch (err) {
    await client.query("ROLLBACK");
    return { success: false, error: (err as Error).message };
  } finally {
    client.release();
  }
}

router.post("/scrape/staged/:id/approve", async (req: Request, res: Response): Promise<void> => {
  try {
    const id = parseInt(req.params.id);
    const [course] = await db.select().from(scrapedCoursesTable).where(eq(scrapedCoursesTable.id, id));
    if (!course) { res.status(404).json({ error: "Not found" }); return; }

    const result = await approveSingleCourse(course);
    if (result.success) {
      res.json({ success: true, courseId: result.courseId });
    } else {
      res.status(500).json({ error: result.error });
    }
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/scrape/staged/approve-all", async (req: Request, res: Response): Promise<void> => {
  try {
    const { jobId } = req.body as { jobId: string };
    const courses = await db.select().from(scrapedCoursesTable)
      .where(and(eq(scrapedCoursesTable.scrapeJobId, jobId), eq(scrapedCoursesTable.status, "pending")));

    let approved = 0;
    let failed = 0;
    for (const course of courses) {
      const result = await approveSingleCourse(course);
      if (result.success) approved++; else failed++;
    }

    res.json({ approved, failed, total: courses.length });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/scrape/staged/reject-all", async (req: Request, res: Response): Promise<void> => {
  try {
    const { jobId } = req.body as { jobId: string };
    await db.delete(scrapedCoursesTable)
      .where(and(eq(scrapedCoursesTable.scrapeJobId, jobId), eq(scrapedCoursesTable.status, "pending")));
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.post("/scrape/preview", async (req: Request, res: Response): Promise<void> => {
  const { url } = req.body as { url: string };
  if (!url) { res.status(400).json({ error: "URL is required" }); return; }
  if (!GEMINI_API_KEY) { res.status(500).json({ error: "GEMINI_API_KEY not configured" }); return; }

  try {
    const html = await fetchPage(url);
    const content = extractFullPageContent(html, url);
    const analysis = await analyzePage(content);
    res.json(analysis);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

export default router;
