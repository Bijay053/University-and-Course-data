import { Router, type IRouter, type Request, type Response } from "express";
import * as cheerio from "cheerio";
import { pool, db, universitiesTable } from "@workspace/db";
import { eq } from "drizzle-orm";

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
  academicLevel?: string;
  academicScore?: number;
  scoreType?: string;
  academicCountry?: string;
  otherRequirement?: string;
  scholarship?: string;
}

interface ScrapeJob {
  id: string;
  status: "running" | "completed" | "failed";
  logs: { event: string; [key: string]: unknown }[];
  imported: number;
  skipped: number;
  errors: number;
  totalFound: number;
  current: number;
  startedAt: number;
  completedAt?: number;
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
  $("script, style, noscript, iframe, svg, nav, footer, header, .cookie, .chat, .popup").remove();
  $("[style*='display:none'], [style*='display: none'], .hidden, [aria-hidden='true']").remove();

  const sections: string[] = [];
  const mainContent = $("main, [role='main'], .content, .course-detail, .course-info, article").first();
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
    result = target.text().replace(/\s+/g, " ").trim().slice(0, 4000);
  }

  return `URL: ${url}\n\n${result.slice(0, 5000)}`;
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

function findRelatedPages(html: string, courseUrl: string): { fees?: string; requirements?: string; entry?: string } {
  const $ = cheerio.load(html);
  const origin = new URL(courseUrl).origin;
  const result: { fees?: string; requirements?: string; entry?: string } = {};

  $("a[href]").each((_, el) => {
    const href = $(el).attr("href") || "";
    const text = $(el).text().trim().toLowerCase();
    try {
      const fullUrl = new URL(href, origin).toString();
      if (!fullUrl.startsWith("http")) return;

      if (!result.fees && (
        /\b(international|overseas)\s*(fee|tuition|cost)/i.test(text) ||
        (/\b(fee|tuition|cost)/i.test(text) && !/domestic/i.test(text))
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
  const ieltsMatch = text.match(/IELTS[:\s]*(?:overall\s*)?(\d+(?:\.\d+)?)/i);
  if (ieltsMatch) data.ieltsOverall = parseFloat(ieltsMatch[1]);

  const subPatterns: { key: string; pattern: RegExp }[] = [
    { key: "ieltsListening", pattern: /(?:listening)[:\s]*(\d+(?:\.\d+)?)/i },
    { key: "ieltsSpeaking", pattern: /(?:speaking)[:\s]*(\d+(?:\.\d+)?)/i },
    { key: "ieltsWriting", pattern: /(?:writing)[:\s]*(\d+(?:\.\d+)?)/i },
    { key: "ieltsReading", pattern: /(?:reading)[:\s]*(\d+(?:\.\d+)?)/i },
  ];
  for (const { key, pattern } of subPatterns) {
    const m = text.match(pattern);
    if (m) (data as any)[key] = parseFloat(m[1]);
  }

  const noBand = text.match(/(?:no\s*(?:band|individual|sub)[^.]*?(?:below|less\s*than|lower\s*than)\s*)(\d+(?:\.\d+)?)/i);
  if (noBand && data.ieltsOverall) {
    const min = parseFloat(noBand[1]);
    if (!data.ieltsListening) data.ieltsListening = min;
    if (!data.ieltsSpeaking) data.ieltsSpeaking = min;
    if (!data.ieltsWriting) data.ieltsWriting = min;
    if (!data.ieltsReading) data.ieltsReading = min;
  }

  const pteMatch = text.match(/PTE[:\s]*(?:Academic[:\s]*)?(?:overall\s*)?(\d+)/i);
  if (pteMatch) data.pteOverall = parseInt(pteMatch[1]);

  const toeflMatch = text.match(/TOEFL[:\s]*(?:iBT[:\s]*)?(?:overall\s*)?(\d+)/i);
  if (toeflMatch) data.toeflOverall = parseInt(toeflMatch[1]);
}

function extractIntakeMonths(text: string, data: Partial<CourseData>) {
  const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const intakeMonths: string[] = [];
  const intakeSection = text.match(/(?:intake|start|commencement|commence|entry)[^.]*?(January|February|March|April|May|June|July|August|September|October|November|December)/gi);
  if (intakeSection) {
    for (const section of intakeSection) {
      for (const m of months) {
        if (section.toLowerCase().includes(m.toLowerCase()) && !intakeMonths.includes(m)) intakeMonths.push(m);
      }
    }
  }
  if (intakeMonths.length > 0) data.intakeMonths = intakeMonths;
}

async function enrichFromRelatedPages(courseData: Partial<CourseData>, relatedPages: { fees?: string; requirements?: string; entry?: string }) {
  const needsFees = !courseData.internationalFee;
  const needsEnglish = !courseData.ieltsOverall;

  const pagesToFetch: { url: string; type: string }[] = [];

  if (needsFees && relatedPages.fees) pagesToFetch.push({ url: relatedPages.fees, type: "fees" });
  if (needsEnglish && relatedPages.entry) pagesToFetch.push({ url: relatedPages.entry, type: "english" });
  if ((needsFees || needsEnglish) && relatedPages.requirements) pagesToFetch.push({ url: relatedPages.requirements, type: "requirements" });

  for (const page of pagesToFetch) {
    try {
      const html = await fetchPage(page.url);
      const text = cheerio.load(html)("body").text();

      if (page.type === "fees" || page.type === "requirements") {
        if (!courseData.internationalFee) extractInternationalFees(text, courseData);
      }
      if (page.type === "english" || page.type === "requirements") {
        if (!courseData.ieltsOverall) extractEnglishRequirements(text, courseData);
      }
      if (!courseData.intakeMonths?.length) extractIntakeMonths(text, courseData);
    } catch {}
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

const SINGLE_EXTRACT_PROMPT = `Extract course data from this page. ONLY extract INTERNATIONAL student fees, NEVER domestic fees. If only domestic fees are shown, set internationalFee to null.

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
  "feeTerm": "<Annual|Total|Semester>",
  "currency": "<AUD|GBP|USD>",
  "ieltsOverall": <number|null>, "ieltsListening": <number|null>, "ieltsSpeaking": <number|null>, "ieltsWriting": <number|null>, "ieltsReading": <number|null>,
  "pteOverall": <number|null>, "toeflOverall": <number|null>,
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

async function saveCourse(courseData: CourseData, uniId: number): Promise<boolean> {
  if (!courseData.courseName) return false;

  const dup = await pool.query(
    "SELECT id FROM courses WHERE university_id=$1 AND name=$2 LIMIT 1",
    [uniId, courseData.courseName],
  );
  if (dup.rows.length > 0) return false;

  const cRes = await pool.query(
    `INSERT INTO courses (university_id, name, category, sub_category, course_website, duration, duration_term, study_mode, degree_level, study_load, language, description, other_requirement, status)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'active') RETURNING id`,
    [
      uniId, courseData.courseName, courseData.category || null, courseData.subCategory || null,
      courseData.courseWebsite || null, courseData.duration || null, courseData.durationTerm || null,
      courseData.studyMode || null, courseData.degreeLevel || null, courseData.studyLoad || null,
      courseData.language || null, courseData.description || null, courseData.otherRequirement || null,
    ],
  );
  const courseId = cRes.rows[0].id;

  if (courseData.intakeMonths?.length) {
    for (const m of courseData.intakeMonths) {
      await pool.query("INSERT INTO intakes (course_id, intake_month, intake_day) VALUES ($1,$2,$3)", [courseId, m, courseData.intakeDays || null]);
    }
  }

  if (courseData.internationalFee) {
    await pool.query(
      "INSERT INTO fees (course_id, international_fee, fee_term, fee_year, currency) VALUES ($1,$2,$3,$4,$5)",
      [courseId, courseData.internationalFee, courseData.feeTerm || null, courseData.feeYear || null, courseData.currency || null],
    );
  }

  for (const test of ["ielts", "pte", "toefl"] as const) {
    const overall = (courseData as any)[`${test}Overall`] as number | undefined;
    const listening = (courseData as any)[`${test}Listening`] as number | undefined;
    const speaking = (courseData as any)[`${test}Speaking`] as number | undefined;
    const writing = (courseData as any)[`${test}Writing`] as number | undefined;
    const reading = (courseData as any)[`${test}Reading`] as number | undefined;
    if (overall || listening || speaking || writing || reading) {
      await pool.query(
        "INSERT INTO english_requirements (course_id, test_type, listening, speaking, writing, reading, overall) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        [courseId, test.toUpperCase(), listening || null, speaking || null, writing || null, reading || null, overall || null],
      );
    }
  }

  if (courseData.academicLevel || courseData.academicScore || courseData.academicCountry) {
    await pool.query(
      "INSERT INTO academic_requirements (course_id, academic_level, academic_score, score_type, academic_country) VALUES ($1,$2,$3,$4,$5)",
      [courseId, courseData.academicLevel || null, courseData.academicScore || null, courseData.scoreType || null, courseData.academicCountry || null],
    );
  }

  if (courseData.scholarship) {
    await pool.query("INSERT INTO scholarships (course_id, name, details) VALUES ($1,$2,$3)", [courseId, "Scholarship", courseData.scholarship]);
  }

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

async function scrapeCourseBatch(
  courseLinks: { url: string; name: string }[],
  uniId: number,
  job: ScrapeJob,
  maxCourses: number,
) {
  const max = Math.min(courseLinks.length, maxCourses);
  job.totalFound = courseLinks.length;

  const batchSize = 10;
  let classifyBatch: { index: number; name: string; existing: Partial<CourseData> }[] = [];
  let pendingCourses: { index: number; data: CourseData }[] = [];

  for (let i = 0; i < max; i++) {
    const link = courseLinks[i];
    job.current = i + 1;
    addLog(job, "progress", { current: i + 1, total: max, courseName: link.name, message: `Fetching ${i + 1}/${max}: ${link.name}` });

    try {
      const cHtml = await fetchPage(link.url);
      const cheerioData = extractWithCheerio(cHtml, link.url, link.name);

      const relatedPages = findRelatedPages(cHtml, link.url);
      if (relatedPages.fees || relatedPages.requirements || relatedPages.entry) {
        addLog(job, "status", { message: `Checking related pages for ${link.name}...`, phase: "enrich" });
        await enrichFromRelatedPages(cheerioData, relatedPages);
      }

      const hasFees = !!cheerioData.internationalFee;
      const hasIelts = !!cheerioData.ieltsOverall;
      const hasDuration = !!cheerioData.duration;

      if (hasFees || hasIelts || hasDuration) {
        const courseData: CourseData = {
          courseName: cheerioData.courseName || link.name,
          courseWebsite: link.url,
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
          toeflOverall: cheerioData.toeflOverall,
          intakeMonths: cheerioData.intakeMonths,
        };

        classifyBatch.push({ index: i, name: link.name, existing: courseData });
        pendingCourses.push({ index: i, data: courseData });
      } else {
        const compactContent = extractCompactContent(cHtml, link.url);
        const cData = await extractCourseFromPage(compactContent, link.name);
        if (cData) {
          cData.courseWebsite = cData.courseWebsite || link.url;
          const saved = await saveCourse(cData, uniId);
          if (saved) { job.imported++; addLog(job, "course", { name: cData.courseName, status: "imported", index: i + 1 }); }
          else { job.skipped++; addLog(job, "course", { name: cData.courseName, status: "skipped", index: i + 1 }); }
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

        const saved = await saveCourse(pending.data, uniId);
        if (saved) { job.imported++; addLog(job, "course", { name: pending.data.courseName, status: "imported", index: pending.index + 1 }); }
        else { job.skipped++; addLog(job, "course", { name: pending.data.courseName, status: "skipped", index: pending.index + 1 }); }
      }

      classifyBatch = [];
      pendingCourses = [];
    }

    if (i % 3 === 2) await new Promise((r) => setTimeout(r, 200));
  }
}

async function runScrapeJob(job: ScrapeJob, url: string, uniId: number) {
  try {
    addLog(job, "status", { message: `Fetching ${url}...`, phase: "fetch" });

    let html: string;
    try {
      html = await fetchPage(url);
    } catch (err) {
      addLog(job, "error", { message: `Failed to fetch URL: ${(err as Error).message}` });
      job.status = "failed";
      job.completedAt = Date.now();
      return;
    }

    addLog(job, "status", { message: "Analyzing page with AI (1 call)...", phase: "analyze" });
    const pageContent = extractFullPageContent(html, url);
    const analysis = await analyzePage(pageContent);

    if (analysis.pageType === "unknown") {
      addLog(job, "status", { message: "No course data in static HTML. Discovering hidden API endpoints...", phase: "discover" });

      const apiCourses = await tryDiscoverApiEndpoints(html, url, job);
      if (apiCourses && apiCourses.length > 0) {
        addLog(job, "status", {
          message: `Found ${apiCourses.length} courses via hidden API. Extracting details...`,
          phase: "extract",
          totalCourses: apiCourses.length,
        });
        await scrapeCourseBatch(apiCourses, uniId, job, 300);
        addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }

      const $ = cheerio.load(html);
      const courseLinks: { url: string; name: string }[] = [];
      const seen = new Set<string>();
      $("a[href]").each((_, el) => {
        const href = $(el).attr("href") || "";
        const text = $(el).text().trim();
        try {
          const fullUrl = new URL(href, new URL(url).origin).toString();
          const lower = fullUrl.toLowerCase();
          if (
            text.length > 5 && text.length < 200 && !seen.has(fullUrl) &&
            (lower.includes("/course") || lower.includes("/program") || lower.includes("/bachelor") ||
             lower.includes("/master") || lower.includes("/diploma") || lower.includes("/study/") ||
             /\b(ba|bsc|ma|msc|mba|phd|bed|beng|llb)\b/i.test(text) ||
             /\b(bachelor|master|graduate|diploma|certificate)\b/i.test(text))
          ) {
            seen.add(fullUrl);
            courseLinks.push({ url: fullUrl, name: text.replace(/\s+/g, " ") });
          }
        } catch {}
      });

      if (courseLinks.length > 0) {
        addLog(job, "status", { message: `Found ${courseLinks.length} course links. Extracting...`, phase: "extract", totalCourses: courseLinks.length });
        await scrapeCourseBatch(courseLinks, uniId, job, 100);
        addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
        job.status = "completed";
        job.completedAt = Date.now();
        return;
      }

      addLog(job, "error", { message: "This page uses JavaScript to load courses dynamically. Try pasting a direct course page URL instead." });
      job.status = "failed";
      job.completedAt = Date.now();
      return;
    }

    if (analysis.pageType === "detail") {
      addLog(job, "status", { message: "Found single course page. Extracting...", phase: "extract" });
      const cheerioData = extractWithCheerio(html, url, "");

      const relatedPages = findRelatedPages(html, url);
      if (relatedPages.fees || relatedPages.requirements || relatedPages.entry) {
        addLog(job, "status", { message: "Checking related pages for fees/requirements...", phase: "enrich" });
        await enrichFromRelatedPages(cheerioData, relatedPages);
      }

      const compactContent = extractCompactContent(html, url);
      const aiData = await extractCourseFromPage(compactContent, cheerioData.courseName || "course");

      if (aiData) {
        for (const [key, val] of Object.entries(cheerioData)) {
          if (val !== undefined && val !== null && !(aiData as any)[key]) {
            (aiData as any)[key] = val;
          }
        }
        aiData.courseWebsite = aiData.courseWebsite || url;
        const saved = await saveCourse(aiData, uniId);
        job.totalFound = 1;
        if (saved) job.imported = 1; else job.skipped = 1;
        addLog(job, "course", { name: aiData.courseName, status: saved ? "imported" : "skipped (duplicate)" });
        addLog(job, "done", { totalFound: 1, imported: job.imported, skipped: job.skipped, errors: 0 });
      } else {
        addLog(job, "error", { message: "Could not extract course data from this page." });
      }
      job.status = "completed";
      job.completedAt = Date.now();
      return;
    }

    if (analysis.pageType === "listing" && analysis.courseLinks?.length) {
      const courseLinks = analysis.courseLinks.filter((l) => l.url && l.name);
      addLog(job, "status", { message: `Found ${courseLinks.length} courses. Extracting...`, phase: "extract", totalCourses: courseLinks.length });
      await scrapeCourseBatch(courseLinks, uniId, job, 200);
      addLog(job, "done", { totalFound: job.totalFound, imported: job.imported, skipped: job.skipped, errors: job.errors });
      job.status = "completed";
      job.completedAt = Date.now();
      return;
    }

    addLog(job, "error", { message: "Could not extract any course data from this page." });
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

    scrapeJobs.set(jobId, job);
    addLog(job, "status", { message: `Using university: ${uniName} (ID: ${uniId})` });

    runScrapeJob(job, url, uniId).catch((err) => {
      addLog(job, "error", { message: `Fatal error: ${(err as Error).message}` });
      job.status = "failed";
      job.completedAt = Date.now();
    });

    res.json({ jobId, message: "Scraping started in background" });
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
    logs: newLogs,
    logIndex: job.logs.length,
  });
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
