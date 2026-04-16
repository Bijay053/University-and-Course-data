import { Router, type IRouter, type Request, type Response } from "express";
import * as cheerio from "cheerio";
import { pool, db, universitiesTable, scrapedCoursesTable } from "@workspace/db";
import { eq, and } from "drizzle-orm";

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

interface ScrapeJob {
  id: string;
  status: "running" | "completed" | "failed" | "stopped";
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
  discoveredConfig?: ScrapeConfig;
}

const scrapeJobs = new Map<string, ScrapeJob>();

function addLog(job: ScrapeJob, event: string, data: Record<string, unknown> = {}) {
  job.logs.push({ event, ...data });
  if (job.logs.length > 2000) job.logs = job.logs.slice(-1500);
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

async function fetchPage(url: string): Promise<string> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const resp = await fetch(url, {
      signal: controller.signal,
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
      },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} for ${url}`);
    return await resp.text();
  } finally {
    clearTimeout(timeout);
  }
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

function extractWithCheerio(html: string, url: string, name: string): Partial<CourseData> {
  const $ = cheerio.load(html);
  const text = $("body").text();
  const data: Partial<CourseData> = { courseName: name, courseWebsite: url, language: "English" };

  const durMatch = text.match(/(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:full[- ]?time)?/i);
  if (durMatch) { data.duration = parseFloat(durMatch[1]); data.durationTerm = "Year"; }
  else {
    const mMatch = text.match(/(\d+)\s*months?\s*(?:full[- ]?time)?/i);
    if (mMatch) { data.duration = parseInt(mMatch[1]); data.durationTerm = "Month"; }
    else {
      const wMatch = text.match(/(\d+)\s*weeks?\s*(?:full[- ]?time)?/i);
      if (wMatch) { data.duration = parseInt(wMatch[1]); data.durationTerm = "Week"; }
    }
  }

  if (/full[- ]?time\s*(and|or|\/)\s*part[- ]?time/i.test(text)) data.studyLoad = "Full Time";
  else if (/full[- ]?time/i.test(text)) data.studyLoad = "Full Time";
  else if (/part[- ]?time/i.test(text)) data.studyLoad = "Part Time";

  if (/on[- ]?campus\s*(and|or|\/)\s*online/i.test(text)) data.studyMode = "Blended";
  else if (/blended|hybrid/i.test(text)) data.studyMode = "Blended";
  else if (/online\s*(only|delivery)/i.test(text)) data.studyMode = "Online";
  else if (/on[- ]?campus/i.test(text)) data.studyMode = "On Campus";

  const lower = name.toLowerCase();
  if (/\bphd\b|doctor of philosophy/i.test(lower)) data.degreeLevel = "PhD";
  else if (/\bmaster\b|^m[a-z]{1,3}\b/i.test(lower)) data.degreeLevel = "Master";
  else if (/\bbachelor\b|^b[a-z]{1,3}\b/i.test(lower)) data.degreeLevel = "Bachelor";
  else if (/\bgraduate\s*(cert|dip)/i.test(lower)) data.degreeLevel = "Graduate Certificate & Diploma";
  else if (/\b(certificate|diploma)\b/i.test(lower)) data.degreeLevel = "Certificate & Diploma";
  else if (/\bassociate\s*degree/i.test(lower)) data.degreeLevel = "Associate Degree";

  extractInternationalFees(text, data);
  extractEnglishRequirements(text, data);
  extractIntakeMonths(text, data);

  const desc = $("meta[name='description']").attr("content") || $("meta[property='og:description']").attr("content") || "";
  if (desc) data.description = desc.slice(0, 500);

  return data;
}

function extractInternationalFees(text: string, data: Partial<CourseData>) {
  const intlSection = text.match(/international[^]*?(?:fee|tuition|cost)[^]*?[\$A]\s*([\d,]+)/i);
  if (intlSection) {
    const fee = parseInt(intlSection[1].replace(/,/g, ""));
    if (fee > 1000 && fee < 200000) {
      data.internationalFee = fee;
      data.currency = "AUD";
      data.feeTerm = /semester/i.test(intlSection[0]) ? "Semester" : /total/i.test(intlSection[0]) ? "Total" : "Annual";
      return;
    }
  }

  const feePatterns = [
    /(?:international|overseas)\s*(?:student\s*)?(?:fee|tuition|cost)[:\s]*\$?([\d,]+)/i,
    /(?:international|overseas)[^.]*?\$\s*([\d,]+)/i,
    /(?:international|overseas)[^.]*?(?:AUD|A\$)\s*([\d,]+)/i,
  ];
  for (const fp of feePatterns) {
    const fm = text.match(fp);
    if (fm) {
      const fee = parseInt(fm[1].replace(/,/g, ""));
      if (fee > 1000 && fee < 200000) {
        data.internationalFee = fee;
        data.currency = "AUD";
        data.feeTerm = /semester/i.test(fm[0]) ? "Semester" : /total/i.test(fm[0]) ? "Total" : "Annual";
        return;
      }
    }
  }

  const genericFee = text.match(/(?:tuition|fee|cost)[:\s]*(?:AUD|A\$|\$)\s*([\d,]+)/i);
  if (genericFee && !/domestic/i.test(genericFee[0])) {
    const fee = parseInt(genericFee[1].replace(/,/g, ""));
    if (fee > 5000 && fee < 200000) {
      data.internationalFee = fee;
      data.currency = "AUD";
      data.feeTerm = /semester/i.test(genericFee[0]) ? "Semester" : /total/i.test(genericFee[0]) ? "Total" : "Annual";
    }
  }
}

function extractEnglishRequirements(text: string, data: Partial<CourseData>) {
  const ieltsSection = text.match(/IELTS\s*(?:Academic|academic)?[^]*?(?=(?:TOEF|TOFL|TOFEL|PTE|Cambridge|CAE|Duolingo|Pathway|Credit|Recognition|\n\s*\n))/i);
  const ieltsText = ieltsSection ? ieltsSection[0] : text;

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

  if (data.toeflOverall && toeflText && !data.toeflListening) {
    const minScoreMatch = toeflText.match(/minimum\s*scores?[:\s]*Reading\s*(\d+)[,\s]*Listening\s*(\d+)[,\s]*Speaking\s*(\d+)[,\s]*Writing\s*(\d+)/i);
    if (minScoreMatch) {
      data.toeflReading = parseInt(minScoreMatch[1]);
      data.toeflListening = parseInt(minScoreMatch[2]);
      data.toeflSpeaking = parseInt(minScoreMatch[3]);
      data.toeflWriting = parseInt(minScoreMatch[4]);
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
    const noPteBelow = pteText.match(/no\s*(?:score|band|component)[^.]*?(?:below|less\s*than|lower\s*than)\s*(\d+)/i);
    if (noPteBelow) {
      const min = parseInt(noPteBelow[1]);
      if (min >= 30 && min <= 90) {
        if (!data.pteListening) data.pteListening = min;
        if (!data.pteSpeaking) data.pteSpeaking = min;
        if (!data.pteWriting) data.pteWriting = min;
        if (!data.pteReading) data.pteReading = min;
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

function extractIntakeMonths(text: string, data: Partial<CourseData>) {
  const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const intakeMonths: string[] = [];

  const intakeSections = text.match(/(?:intake|start\s*date|commencement|commence|entry\s*point|intake\s*option)[^]*?(?:\n\n|See\s|$)/gi);
  if (intakeSections) {
    for (const section of intakeSections) {
      for (const m of months) {
        if (section.toLowerCase().includes(m.toLowerCase()) && !intakeMonths.includes(m)) intakeMonths.push(m);
      }
    }
  }

  if (intakeMonths.length === 0) {
    const monthListPattern = text.match(/(?:intake|start|commencement)[^.]{0,100}?((?:(?:January|February|March|April|May|June|July|August|September|October|November|December)[\s,/and]*)+)/gi);
    if (monthListPattern) {
      for (const section of monthListPattern) {
        for (const m of months) {
          if (section.toLowerCase().includes(m.toLowerCase()) && !intakeMonths.includes(m)) intakeMonths.push(m);
        }
      }
    }
  }

  if (intakeMonths.length > 0) data.intakeMonths = intakeMonths;
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
{"ieltsOverall":<number>,"ieltsListening":<number>,"ieltsSpeaking":<number>,"ieltsWriting":<number>,"ieltsReading":<number>,"pteOverall":<number>,"pteListening":<number>,"pteSpeaking":<number>,"pteWriting":<number>,"pteReading":<number>,"toeflOverall":<number>,"toeflListening":<number>,"toeflSpeaking":<number>,"toeflWriting":<number>,"toeflReading":<number>,"cambridgeOverall":<number>,"duolingoOverall":<number>,"internationalFee":<number>,"currency":"<AUD|GBP|USD>","feeTerm":"<Annual|Total|Semester|Per Unit>"}
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
Return JSON: {"internationalFee":<number per year or per unit>,"currency":"<AUD|GBP|USD>","feeTerm":"<Annual|Total|Semester|Per Unit>","feeYear":<year>}
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
        if (!courseData.internationalFee) extractInternationalFees(text, courseData);
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
1. ONLY extract INTERNATIONAL student fees, NEVER domestic/local fees. If only domestic fees shown, set internationalFee to null.
2. Look for ALL tab sections (Course Overview, Entry Requirements, Fees, Course Structure etc.) - data may be spread across tabs.
3. Look for fee links (PDFs, fee schedule links) - if you see a link to a fee schedule, note the URL.
4. Look for intake months (January, February, etc.), duration, study mode, and location.
5. Extract ALL English language test requirements visible in text - IELTS, TOEFL iBT, PTE Academic, Cambridge CAE/C1 Advanced, Duolingo. They may be in images which you cannot read.
6. For TOEFL, look for sub-scores per skill (Reading, Listening, Speaking, Writing).

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
  "feeTerm": "<Annual|Total|Semester|Per Unit>",
  "currency": "<AUD|GBP|USD>",
  "ieltsOverall": <number|null>, "ieltsListening": <number|null>, "ieltsSpeaking": <number|null>, "ieltsWriting": <number|null>, "ieltsReading": <number|null>,
  "pteOverall": <number|null>, "pteListening": <number|null>, "pteSpeaking": <number|null>, "pteWriting": <number|null>, "pteReading": <number|null>,
  "toeflOverall": <number|null>, "toeflListening": <number|null>, "toeflSpeaking": <number|null>, "toeflWriting": <number|null>, "toeflReading": <number|null>,
  "cambridgeOverall": <number|null>,
  "duolingoOverall": <number|null>,
  "intakeMonths": ["<month>"],
  "academicLevel": "<required education>",
  "otherRequirement": "<other reqs>",
  "scholarship": "<scholarship info>"
}
IMPORTANT: Only include INTERNATIONAL student fees. Exclude all domestic/local fees. Use null for missing fields.`;

async function extractCourseFromPage(content: string, courseName: string): Promise<CourseData | null> {
  try {
    const text = await geminiChat(SINGLE_EXTRACT_PROMPT, `Course: "${courseName}"\n\n${content}`, 2048);
    const data = JSON.parse(text) as CourseData;
    return data.courseName ? data : null;
  } catch {
    return null;
  }
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

async function stageCourse(courseData: CourseData, uniId: number, jobId: string): Promise<boolean> {
  if (!courseData.courseName) return false;

  const dup = await pool.query(
    "SELECT id FROM scraped_courses WHERE scrape_job_id=$1 AND course_name=$2 LIMIT 1",
    [jobId, courseData.courseName],
  );
  if (dup.rows.length > 0) return false;

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
]);

function isJunkCourseName(name: string): boolean {
  const lower = name.toLowerCase().trim();
  if (JUNK_LINK_NAMES.has(lower)) return true;
  if (lower.length < 6) return true;
  if (lower.length > 200) return true;
  if (/^(all|view|see|find|browse|search|show)\s/i.test(lower)) return true;
  if (/^(our|the|a)\s+(course|program|degree)/i.test(lower)) return true;
  if (!/[a-z]/i.test(lower)) return true;
  return false;
}

function isCourseUrl(urlStr: string): boolean {
  const lower = urlStr.toLowerCase();
  return (
    lower.includes("/course") || lower.includes("/program") || lower.includes("/bachelor") ||
    lower.includes("/master") || lower.includes("/diploma") || lower.includes("/study/") ||
    lower.includes("/graduate-diploma") || lower.includes("/certificate") || lower.includes("/degree") ||
    lower.includes("/phd") || lower.includes("/mba")
  );
}

function isCourseText(text: string): boolean {
  return /\b(bachelor|master|graduate\s*diploma|diploma|certificate|doctor|phd|mba|associate)\b/i.test(text) ||
    /\b(ba|bsc|ma|msc|mba|bed|beng|llb|med)\b/i.test(text);
}

async function discoverCourseLinksFromSitemap(origin: string, job: ScrapeJob): Promise<{ url: string; name: string }[]> {
  const courses: { url: string; name: string }[] = [];
  const seen = new Set<string>();

  try {
    const sitemapUrls = [`${origin}/sitemap.xml`, `${origin}/sitemap_index.xml`];

    for (const smUrl of sitemapUrls) {
      try {
        const xml = await fetchPage(smUrl);

        const nestedSitemaps = [...xml.matchAll(/<loc>([^<]+\.xml[^<]*)<\/loc>/gi)].map(m => m[1]);
        const allXmls = nestedSitemaps.length > 0 ? nestedSitemaps : [smUrl];

        for (const sitemapFile of allXmls) {
          try {
            const content = sitemapFile === smUrl ? xml : await fetchPage(sitemapFile);
            const locs = [...content.matchAll(/<loc>([^<]+)<\/loc>/gi)].map(m => m[1]);

            for (const loc of locs) {
              if (seen.has(loc)) continue;
              if (isCourseUrl(loc)) {
                seen.add(loc);
                const pathParts = new URL(loc).pathname.split("/").filter(Boolean);
                const rawName = pathParts[pathParts.length - 1]
                  .replace(/[-_]/g, " ")
                  .replace(/\b\w/g, c => c.toUpperCase());
                if (!isJunkCourseName(rawName)) {
                  courses.push({ url: loc, name: rawName });
                }
              }
            }
          } catch {}
        }
        if (courses.length > 0) break;
      } catch {}
    }
  } catch {}

  if (courses.length > 0) {
    addLog(job, "status", { message: `Sitemap: found ${courses.length} course URLs`, phase: "discover" });
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

  const sitemapCourses = await discoverCourseLinksFromSitemap(origin, job);
  for (const c of sitemapCourses) {
    if (!seen.has(c.url)) {
      seen.add(c.url);
      allCourses.push(c);
    }
  }

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
        const fullUrl = href.startsWith("http") ? href : new URL(href, origin).toString();
        if (!fullUrl.startsWith(origin)) return;
        if (visited.has(fullUrl)) return;
        visited.add(fullUrl);

        if (!result.feesPdf && /\.pdf/i.test(fullUrl) && /fee|tuition/i.test(fullUrl + " " + text)) {
          result.feesPdf = fullUrl;
        }
        if (!result.feePage && /\b(tuition|fee)\b/i.test(text) && /\b(international|overseas|student|schedule)\b/i.test(text + " " + fullUrl) && !/fee.?help|scholarship|refund|payment.?plan/i.test(fullUrl + " " + text)) {
          result.feePage = fullUrl;
        }
        if (!result.feePage && /\b(tuition.?fee|fee.?schedule|international.?fee)\b/i.test(fullUrl) && !/fee.?help|scholarship|refund/i.test(fullUrl)) {
          result.feePage = fullUrl;
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
        const fullUrl = href.startsWith("http") ? href : new URL(href, origin).toString();
        if (!fullUrl.startsWith(origin)) return;

        if (!result.feesPdf && /\.pdf/i.test(fullUrl) && /fee|tuition/i.test(fullUrl + " " + text)) {
          result.feesPdf = fullUrl;
        }
        if (!result.feePage && (/\b(tuition|fee)\b/i.test(text) || /tuition.?fee|fee.?schedule/i.test(fullUrl))) {
          result.feePage = fullUrl;
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
        const resp = await fetch(testUrl, { method: "HEAD", headers: { "User-Agent": "Mozilla/5.0" }, signal: AbortSignal.timeout(5000) });
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
    const html = await fetchPage(feePage);
    cache.html = html;
    cache.text = cheerio.load(html)("body").text();
    return cache.text;
  } catch {
    return "";
  }
}

async function extractFeeFromUniversityPage(feePage: string, courseName: string, courseData: Partial<CourseData>, cache: UniversityFeeCache, noAi = false): Promise<void> {
  if (courseData.internationalFee) return;

  const text = await getUniversityFeePageText(feePage, cache);
  if (!text) return;

  const courseNameLower = courseName.toLowerCase().replace(/\s+/g, "\\s*");
  const courseWords = courseName.split(/\s+/).filter(w => w.length > 3);

  const intlSectionMatch = text.match(/international[^]*?(?=domestic|$)/i);
  const searchText = intlSectionMatch ? intlSectionMatch[0] : text;

  for (const word of courseWords) {
    const regex = new RegExp(`${word}[^]*?(?:A\\$|\\$|£|€)\\s*([\\d,]+)`, "i");
    const m = searchText.match(regex);
    if (m) {
      const fee = parseInt(m[1].replace(/,/g, ""));
      if (fee > 1000 && fee < 200000) {
        courseData.internationalFee = fee;
        courseData.currency = /£/.test(m[0]) ? "GBP" : /€/.test(m[0]) ? "EUR" : "AUD";
        courseData.feeTerm = /semester/i.test(m[0]) ? "Semester" : /per\s*unit/i.test(m[0]) ? "Per Unit" : "Annual";
        return;
      }
    }
  }

  if (!noAi && !courseData.internationalFee && GEMINI_API_KEY) {
    try {
      const prompt = `From this fee schedule page text, find the INTERNATIONAL student tuition fee for the course "${courseName}".
Return JSON: {"internationalFee":<number>,"currency":"<AUD|GBP|USD|EUR>","feeTerm":"<Annual|Total|Semester|Per Unit>"}
Use null if not found. ONLY international fees.`;
      const trimmedText = searchText.slice(0, 8000);
      const result = await geminiChat(prompt, trimmedText, 256);
      const parsed = JSON.parse(result);
      if (parsed.internationalFee && parsed.internationalFee > 1000) {
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

async function scrapeCourseBatch(
  courseLinks: { url: string; name: string }[],
  uniId: number,
  job: ScrapeJob,
  maxCourses: number,
  jobId: string,
  uniPages?: { feePage?: string; feesPdf?: string; requirementsPage?: string; entryPage?: string },
) {
  const max = Math.min(courseLinks.length, maxCourses);
  job.totalFound = courseLinks.length;

  const feeCache: UniversityFeeCache = { fetched: false };
  let uniReqsText: string | null = null;

  if (uniPages?.requirementsPage || uniPages?.entryPage) {
    try {
      const reqUrl = uniPages.entryPage || uniPages.requirementsPage!;
      const reqHtml = await fetchPage(reqUrl);
      uniReqsText = cheerio.load(reqHtml)("body").text();
    } catch {}
  }

  const batchSize = 10;
  let classifyBatch: { index: number; name: string; existing: Partial<CourseData> }[] = [];
  let pendingCourses: { index: number; data: CourseData }[] = [];

  for (let i = 0; i < max; i++) {
    if (job.stopped) {
      addLog(job, "status", { message: `Stopped after ${i} courses (${job.imported} staged)` });
      break;
    }
    const link = courseLinks[i];
    job.current = i + 1;
    addLog(job, "progress", { current: i + 1, total: max, courseName: link.name, message: `Fetching ${i + 1}/${max}: ${link.name}` });

    try {
      const cHtml = await fetchPage(link.url);
      const cheerioData = extractWithCheerio(cHtml, link.url, link.name);

      const relatedPages = findRelatedPages(cHtml, link.url);
      if (relatedPages.fees || relatedPages.requirements || relatedPages.entry || relatedPages.feesPdf) {
        addLog(job, "status", { message: `Checking related pages/PDFs for ${link.name}...`, phase: "enrich" });
        await enrichFromRelatedPages(cheerioData, relatedPages, cHtml, link.url);
      } else if (!(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall) || !cheerioData.internationalFee) {
        const images = findImageUrls(cHtml, link.url);
        if (images.length > 0) {
          addLog(job, "status", { message: `Analyzing images for data in ${link.name}...`, phase: "enrich" });
          await enrichFromRelatedPages(cheerioData, relatedPages, cHtml, link.url);
        }
      }

      if (!cheerioData.internationalFee && uniPages?.feePage) {
        await extractFeeFromUniversityPage(uniPages.feePage, link.name, cheerioData, feeCache);
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

      if (uniReqsText && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)) {
        extractEnglishRequirements(uniReqsText, cheerioData);
      }

      const hasFees = !!cheerioData.internationalFee;
      const hasEnglish = !!(cheerioData.ieltsOverall || cheerioData.pteOverall || cheerioData.toeflOverall || cheerioData.cambridgeOverall);
      const hasDuration = !!cheerioData.duration;

      if (hasFees || hasEnglish || hasDuration) {
        const courseData = cheerioToCourseData(cheerioData, link.name, link.url);

        classifyBatch.push({ index: i, name: link.name, existing: courseData });
        pendingCourses.push({ index: i, data: courseData });
      } else {
        let cData: CourseData | null = null;
        try {
          const compactContent = extractCompactContent(cHtml, link.url);
          cData = await extractCourseFromPage(compactContent, link.name);
        } catch (aiErr) {
          console.log(`AI extraction failed for ${link.name}: ${(aiErr as Error).message}`);
        }

        if (cData) {
          for (const [key, val] of Object.entries(cheerioData)) {
            if (val !== undefined && val !== null && !(cData as any)[key]) {
              (cData as any)[key] = val;
            }
          }
          cData.courseWebsite = cData.courseWebsite || link.url;
          const saved = await stageCourse(cData, uniId, jobId);
          if (saved) { job.imported++; addLog(job, "course", { name: cData.courseName, status: "staged", index: i + 1 }); }
          else { job.skipped++; addLog(job, "course", { name: cData.courseName, status: "skipped", index: i + 1 }); }
        } else if (cheerioData.courseName || link.name) {
          const fallbackData = cheerioToCourseData(cheerioData, link.name, link.url);
          const saved = await stageCourse(fallbackData, uniId, jobId);
          if (saved) { job.imported++; addLog(job, "course", { name: fallbackData.courseName, status: "staged (cheerio only)", index: i + 1 }); }
          else { job.skipped++; addLog(job, "course", { name: fallbackData.courseName, status: "skipped", index: i + 1 }); }
        } else {
          job.errors++;
          addLog(job, "course", { name: link.name, status: "error", message: "Could not extract data", index: i + 1 });
        }
      }
    } catch (err) {
      job.errors++;
      addLog(job, "course", { name: link.name, status: "error", message: (err as Error).message, index: i + 1 });
    }

    if (classifyBatch.length >= batchSize || (i === max - 1 && classifyBatch.length > 0)) {
      addLog(job, "status", { message: `Classifying batch of ${classifyBatch.length} courses with AI...`, phase: "classify" });
      const classifications = await batchClassify(classifyBatch);

      for (const pending of pendingCourses) {
        const extra = classifications.get(pending.index);
        if (extra) {
          if (extra.category && !pending.data.category) pending.data.category = extra.category;
          if (extra.subCategory && !pending.data.subCategory) pending.data.subCategory = extra.subCategory;
          if (extra.degreeLevel && !pending.data.degreeLevel) pending.data.degreeLevel = extra.degreeLevel;
          if (extra.description && !pending.data.description) pending.data.description = extra.description;
        }

        const saved = await stageCourse(pending.data, uniId, jobId);
        if (saved) { job.imported++; addLog(job, "course", { name: pending.data.courseName, status: "staged", index: pending.index + 1 }); }
        else { job.skipped++; addLog(job, "course", { name: pending.data.courseName, status: "skipped", index: pending.index + 1 }); }
      }

      classifyBatch = [];
      pendingCourses = [];
    }

    if (i % 3 === 2) await new Promise((r) => setTimeout(r, 200));
  }
}

async function tryAlternativeUrls(url: string, job: ScrapeJob): Promise<{ html: string; resolvedUrl: string } | null> {
  const origin = new URL(url).origin;
  const pathname = new URL(url).pathname;

  const parentPath = pathname.split("/").slice(0, -1).join("/") || "/";
  const alternatives = [
    parentPath !== "/" ? `${origin}${parentPath}` : null,
    `${origin}/courses`,
    `${origin}/programs`,
    `${origin}/study`,
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

async function runScrapeJob(job: ScrapeJob, url: string, uniId: number, jobId: string) {
  try {
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

    addLog(job, "status", { message: "Discovering university-level fee & requirements pages...", phase: "discover" });
    const uniPages = await discoverUniversityPages(resolvedUrl, job);

    if (!html) {
      addLog(job, "status", { message: "No direct page available. Scanning sitemap for course URLs...", phase: "discover" });
      const sitemapCourses = await discoverCourseLinksFromSitemap(origin, job);
      if (sitemapCourses.length > 0) {
        addLog(job, "status", { message: `Found ${sitemapCourses.length} courses from sitemap. Extracting...`, phase: "extract", totalCourses: sitemapCourses.length });
        await scrapeCourseBatch(sitemapCourses, uniId, job, 300, jobId, uniPages);
        addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }

      addLog(job, "status", { message: "Crawling site for course pages...", phase: "discover" });
      const crawled = await crawlForCourseLinks(origin, origin, job, 2);
      if (crawled.length > 0) {
        addLog(job, "status", { message: `Found ${crawled.length} courses by crawling. Extracting...`, phase: "extract", totalCourses: crawled.length });
        await scrapeCourseBatch(crawled, uniId, job, 300, jobId, uniPages);
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

    addLog(job, "status", { message: "Analyzing page with AI (1 call)...", phase: "analyze" });
    const pageContent = extractFullPageContent(html, resolvedUrl);
    let analysis: { pageType: string; courseLinks?: { url: string; name: string }[] };
    try {
      analysis = await analyzePage(pageContent);
    } catch (err) {
      addLog(job, "status", { message: `AI analysis failed (${(err as Error).message}). Falling back to HTML scan...`, phase: "fallback" });
      analysis = { pageType: "unknown" };
    }

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

      if (!cheerioData.internationalFee && uniPages.feePage) {
        addLog(job, "status", { message: "Checking university fee page...", phase: "enrich" });
        const singleFeeCache: UniversityFeeCache = { fetched: false };
        await extractFeeFromUniversityPage(uniPages.feePage, cheerioData.courseName || "", cheerioData, singleFeeCache);
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
        for (const [key, val] of Object.entries(cheerioData)) {
          if (val !== undefined && val !== null && !(aiData as any)[key]) {
            (aiData as any)[key] = val;
          }
        }
        aiData.courseWebsite = aiData.courseWebsite || resolvedUrl;
        const saved = await stageCourse(aiData, uniId, jobId);
        job.totalFound = 1;
        if (saved) job.imported = 1; else job.skipped = 1;
        addLog(job, "course", { name: aiData.courseName, status: saved ? "staged" : "skipped (duplicate)" });
        addLog(job, "done", { totalFound: 1, imported: job.imported, skipped: job.skipped, errors: 0 });
      } else {
        addLog(job, "error", { message: "Could not extract course data from this page." });
      }
      job.status = "completed";
      job.completedAt = Date.now();
      return;
    }

    addLog(job, "status", { message: "Searching all sources for course links (page + sitemap + sub-pages)...", phase: "discover" });

    let courseLinks: { url: string; name: string }[] = [];

    if (analysis.pageType === "unknown") {
      const apiCourses = await tryDiscoverApiEndpoints(html, resolvedUrl, job);
      if (apiCourses && apiCourses.length > 0) {
        addLog(job, "status", {
          message: `Found ${apiCourses.length} courses via hidden API. Extracting details...`,
          phase: "extract",
          totalCourses: apiCourses.length,
        });
        await scrapeCourseBatch(apiCourses, uniId, job, 300, jobId, uniPages);
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

      courseLinks = await discoverAllCourseLinks(resolvedUrl, html, job, []);
    } else if (analysis.pageType === "listing" && analysis.courseLinks?.length) {
      const aiLinks = analysis.courseLinks.filter((l) => l.url && l.name);
      courseLinks = await discoverAllCourseLinks(resolvedUrl, html, job, aiLinks);
    }

    if (courseLinks.length > 0) {
      addLog(job, "status", {
        message: `Found ${courseLinks.length} total course links. Extracting details...`,
        phase: "extract",
        totalCourses: courseLinks.length,
      });
      await scrapeCourseBatch(courseLinks, uniId, job, 300, jobId, uniPages);
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
  const { url, universityId, universityName, universityCountry, universityCity } = req.body as {
    url: string;
    universityId?: number;
    universityName?: string;
    universityCountry?: string;
    universityCity?: string;
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
    scrapeJobs.set(jobId, job);
    addLog(job, "status", { message: `Using university: ${uniName} (ID: ${uniId})` });

    await db.update(universitiesTable).set({ scrapeUrl: url }).where(eq(universitiesTable.id, uniId));

    runScrapeJob(job, url, uniId, jobId).catch((err) => {
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

    if (uniPages?.requirementsPage || uniPages?.entryPage) {
      try {
        const reqUrl = uniPages.entryPage || uniPages.requirementsPage!;
        const reqHtml = await fetchPage(reqUrl);
        uniReqsText = cheerio.load(reqHtml)("body").text();
      } catch {}
    }

    const max = config.courseLinks.length;
    job.totalFound = max;
    const batchSize = 10;
    let classifyBatch: { index: number; name: string; existing: Partial<CourseData> }[] = [];
    let pendingCourses: { index: number; data: CourseData }[] = [];

    for (let i = 0; i < max; i++) {
      if (job.stopped) {
        addLog(job, "status", { message: `Stopped after ${i} courses (${job.imported} staged)` });
        break;
      }
      const link = config.courseLinks[i];
      job.current = i + 1;
      addLog(job, "progress", { current: i + 1, total: max, courseName: link.name, message: `Fetching ${i + 1}/${max}: ${link.name}` });

      try {
        const cHtml = await fetchPage(link.url);
        const cheerioData = extractWithCheerio(cHtml, link.url, link.name);

        const relatedPages = findRelatedPages(cHtml, link.url);
        const noAiRelated = { fees: relatedPages.fees, requirements: relatedPages.requirements, entry: relatedPages.entry };
        if (noAiRelated.fees || noAiRelated.requirements || noAiRelated.entry) {
          for (const page of [
            noAiRelated.fees && { url: noAiRelated.fees, type: "fees" as const },
            noAiRelated.entry && { url: noAiRelated.entry, type: "english" as const },
            noAiRelated.requirements && { url: noAiRelated.requirements, type: "requirements" as const },
          ].filter(Boolean) as { url: string; type: string }[]) {
            try {
              const pHtml = await fetchPage(page.url);
              const text = cheerio.load(pHtml)("body").text();
              if (page.type === "fees" || page.type === "requirements") {
                if (!cheerioData.internationalFee) extractInternationalFees(text, cheerioData);
              }
              if (page.type === "english" || page.type === "requirements") {
                extractEnglishRequirements(text, cheerioData);
              }
              if (!cheerioData.intakeMonths?.length) extractIntakeMonths(text, cheerioData);
            } catch {}
          }
        }

        if (!cheerioData.internationalFee && uniPages?.feePage) {
          await extractFeeFromUniversityPage(uniPages.feePage, link.name, cheerioData, feeCache, true);
        }

        if (uniReqsText && !(cheerioData.ieltsOverall && cheerioData.pteOverall && cheerioData.toeflOverall && cheerioData.cambridgeOverall)) {
          extractEnglishRequirements(uniReqsText, cheerioData);
        }

        const courseData = cheerioToCourseData(cheerioData, link.name, link.url);

        classifyBatch.push({ index: i, name: link.name, existing: courseData });
        pendingCourses.push({ index: i, data: courseData });
      } catch (err) {
        job.errors++;
        addLog(job, "course", { name: link.name, status: "error", message: (err as Error).message, index: i + 1 });
      }

      if (classifyBatch.length >= batchSize || (i === max - 1 && classifyBatch.length > 0)) {
        addLog(job, "status", { message: `Staging batch of ${pendingCourses.length} courses (no AI classification)...`, phase: "stage" });

        for (const pending of pendingCourses) {
          const saved = await stageCourse(pending.data, uniId, jobId);
          if (saved) { job.imported++; addLog(job, "course", { name: pending.data.courseName, status: "staged", index: pending.index + 1 }); }
          else { job.skipped++; addLog(job, "course", { name: pending.data.courseName, status: "skipped", index: pending.index + 1 }); }
        }

        classifyBatch = [];
        pendingCourses = [];
      }

      if (i % 3 === 2) await new Promise((r) => setTimeout(r, 200));
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
  });
});

router.post("/scrape/stop/:jobId", (req: Request, res: Response): void => {
  const job = scrapeJobs.get(req.params.jobId);
  if (!job) { res.status(404).json({ error: "Job not found" }); return; }
  if (job.status !== "running") { res.status(400).json({ error: "Job is not running" }); return; }

  job.stopped = true;
  job.status = "stopped";
  job.completedAt = Date.now();
  addLog(job, "status", { message: "Scraping stopped by user" });
  addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });

  res.json({ message: "Scraping stopped", imported: job.imported });
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
