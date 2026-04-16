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

interface PageAnalysis {
  pageType: "listing" | "detail" | "unknown";
  courseLinks?: { url: string; name: string }[];
  courseData?: CourseData;
  totalCoursesFound?: number;
  paginationLinks?: string[];
}

function sendSSE(res: Response, event: string, data: unknown) {
  res.write(`data: ${JSON.stringify({ event, ...data as object })}\n\n`);
}

async function geminiChat(systemPrompt: string, userContent: string): Promise<string> {
  if (!GEMINI_API_KEY) throw new Error("GEMINI_API_KEY not configured");

  const body = JSON.stringify({
    system_instruction: { parts: [{ text: systemPrompt }] },
    contents: [{ parts: [{ text: userContent }] }],
    generationConfig: {
      responseMimeType: "application/json",
      maxOutputTokens: 8192,
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
          if (attempt === 0) {
            await new Promise((r) => setTimeout(r, 5000));
            continue;
          }
          break;
        }

        if (resp.status === 404) {
          console.log(`Gemini model ${model} not available, trying next...`);
          break;
        }

        if (!resp.ok) {
          const errText = await resp.text();
          throw new Error(`Gemini API error ${resp.status}: ${errText.slice(0, 300)}`);
        }

        const data = await resp.json() as any;
        const text = data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
        if (!text) {
          console.log(`Empty response from ${model}, trying next...`);
          break;
        }
        console.log(`Gemini response OK from ${model}`);
        return text;
      } catch (err) {
        if ((err as Error).message.includes("Gemini API error")) throw err;
        console.log(`Gemini ${model} attempt ${attempt + 1} failed: ${(err as Error).message}`);
        if (attempt === 0) {
          await new Promise((r) => setTimeout(r, 3000));
        }
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

function extractPageContent(html: string, url: string): string {
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
        if (fullUrl.startsWith("http")) {
          links.push(`[${text}](${fullUrl})`);
        }
      } catch {}
    }
  });

  const bodyText = $("body").text().replace(/\s+/g, " ").trim();
  const truncatedBody = bodyText.slice(0, 15000);
  const truncatedLinks = links.slice(0, 150).join("\n");

  return `URL: ${url}\n\nPAGE TEXT:\n${truncatedBody}\n\nLINKS ON PAGE:\n${truncatedLinks}`;
}

const ANALYZE_PROMPT = `You are a university course data extractor. Analyze the webpage content and determine:

1. If it's a LISTING page (shows multiple courses with links), extract the course links.
2. If it's a DETAIL page (shows one course in detail), extract all available course data.

Return JSON in this exact format:

For LISTING pages:
{
  "pageType": "listing",
  "totalCoursesFound": <number>,
  "courseLinks": [{"url": "<full URL>", "name": "<course name>"}],
  "paginationLinks": ["<url to next page>"]
}

For DETAIL pages:
{
  "pageType": "detail",
  "courseData": {
    "courseName": "<name>",
    "category": "<broad category like 'Business & Management', 'Engineering & Technology', 'Computer Science & IT', 'Medicine & Health', 'Arts, Humanities & Social Sciences', 'Education & Social Work', 'Architecture, Building & Design', 'Media & Communications', 'Law & Legal Studies', 'Hospitality, Tourism & Events', 'Science & Mathematics', 'Agriculture & Environmental Science'>",
    "subCategory": "<specific sub-category>",
    "courseWebsite": "<course page URL>",
    "duration": <number or null>,
    "durationTerm": "<Year or Month or Week>",
    "studyMode": "<On Campus or Online or Blended>",
    "degreeLevel": "<Bachelor or Master or PhD or Certificate & Diploma or Graduate Certificate & Diploma or Associate Degree or Equivalent>",
    "studyLoad": "<Full Time or Part Time>",
    "language": "<language of instruction>",
    "description": "<brief course description>",
    "intakeMonths": ["January", "March"],
    "internationalFee": <number or null>,
    "feeTerm": "<Annual or Total or Semester>",
    "currency": "<AUD or GBP or USD etc>",
    "ieltsOverall": <number or null>,
    "ieltsListening": <number or null>,
    "ieltsSpeaking": <number or null>,
    "ieltsWriting": <number or null>,
    "ieltsReading": <number or null>,
    "pteOverall": <number or null>,
    "pteListening": <number or null>,
    "pteSpeaking": <number or null>,
    "pteWriting": <number or null>,
    "pteReading": <number or null>,
    "toeflOverall": <number or null>,
    "toeflListening": <number or null>,
    "toeflSpeaking": <number or null>,
    "toeflWriting": <number or null>,
    "toeflReading": <number or null>,
    "academicLevel": "<required prior education level>",
    "academicCountry": "<country if specified>",
    "otherRequirement": "<other admission requirements>",
    "scholarship": "<scholarship info if mentioned>"
  }
}

If the page is neither, return: {"pageType": "unknown"}
Extract ONLY data that is clearly present on the page. Use null for missing fields.
For course links, include the FULL URL (not relative paths).`;

const EXTRACT_PROMPT = (courseName: string) => `You are a university course data extractor. Extract ALL available course information from this webpage for the course "${courseName}".

Return JSON with these fields (use null for any field not found on the page):
{
  "courseName": "<exact course name>",
  "category": "<broad category: Business & Management, Engineering & Technology, Computer Science & IT, Medicine & Health, Arts Humanities & Social Sciences, Education & Social Work, Architecture Building & Design, Media & Communications, Law & Legal Studies, Hospitality Tourism & Events, Science & Mathematics, Agriculture & Environmental Science>",
  "subCategory": "<specific sub-category>",
  "courseWebsite": "<URL of this page>",
  "duration": <number or null>,
  "durationTerm": "<Year or Month or Week>",
  "studyMode": "<On Campus or Online or Blended>",
  "degreeLevel": "<Bachelor or Master or PhD or Certificate & Diploma or Graduate Certificate & Diploma or Associate Degree or Equivalent>",
  "studyLoad": "<Full Time or Part Time>",
  "language": "<language>",
  "description": "<brief description max 500 chars>",
  "intakeMonths": ["January", "September"],
  "internationalFee": <number or null>,
  "feeTerm": "<Annual or Total or Semester>",
  "feeYear": <year number or null>,
  "currency": "<AUD/GBP/USD/etc>",
  "ieltsOverall": <number or null>,
  "ieltsListening": <number or null>,
  "ieltsSpeaking": <number or null>,
  "ieltsWriting": <number or null>,
  "ieltsReading": <number or null>,
  "pteOverall": <number or null>,
  "pteSpeaking": <number or null>,
  "pteWriting": <number or null>,
  "pteReading": <number or null>,
  "pteListening": <number or null>,
  "toeflOverall": <number or null>,
  "toeflListening": <number or null>,
  "toeflSpeaking": <number or null>,
  "toeflWriting": <number or null>,
  "toeflReading": <number or null>,
  "academicLevel": "<required education>",
  "academicScore": <number or null>,
  "scoreType": "<GPA/Percentage/etc>",
  "academicCountry": "<country>",
  "otherRequirement": "<other requirements>",
  "scholarship": "<scholarship info>"
}`;

async function analyzePage(content: string): Promise<PageAnalysis> {
  const text = await geminiChat(ANALYZE_PROMPT, content);
  try {
    return JSON.parse(text) as PageAnalysis;
  } catch {
    return { pageType: "unknown" };
  }
}

async function extractCourseFromPage(content: string, courseName: string): Promise<CourseData | null> {
  try {
    const text = await geminiChat(EXTRACT_PROMPT(courseName), content);
    const data = JSON.parse(text) as CourseData;
    return data.courseName ? data : null;
  } catch {
    return null;
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
      uniId,
      courseData.courseName,
      courseData.category || null,
      courseData.subCategory || null,
      courseData.courseWebsite || null,
      courseData.duration || null,
      courseData.durationTerm || null,
      courseData.studyMode || null,
      courseData.degreeLevel || null,
      courseData.studyLoad || null,
      courseData.language || null,
      courseData.description || null,
      courseData.otherRequirement || null,
    ],
  );
  const courseId = cRes.rows[0].id;

  if (courseData.intakeMonths?.length) {
    for (const m of courseData.intakeMonths) {
      await pool.query(
        "INSERT INTO intakes (course_id, intake_month, intake_day) VALUES ($1,$2,$3)",
        [courseId, m, courseData.intakeDays || null],
      );
    }
  }

  if (courseData.internationalFee) {
    await pool.query(
      "INSERT INTO fees (course_id, international_fee, fee_term, fee_year, currency) VALUES ($1,$2,$3,$4,$5)",
      [courseId, courseData.internationalFee, courseData.feeTerm || null, courseData.feeYear || null, courseData.currency || null],
    );
  }

  for (const test of ["ielts", "pte", "toefl"] as const) {
    const prefix = test;
    const overall = (courseData as any)[`${prefix}Overall`] as number | undefined;
    const listening = (courseData as any)[`${prefix}Listening`] as number | undefined;
    const speaking = (courseData as any)[`${prefix}Speaking`] as number | undefined;
    const writing = (courseData as any)[`${prefix}Writing`] as number | undefined;
    const reading = (courseData as any)[`${prefix}Reading`] as number | undefined;
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
    await pool.query(
      "INSERT INTO scholarships (course_id, name, details) VALUES ($1,$2,$3)",
      [courseId, "Scholarship", courseData.scholarship],
    );
  }

  return true;
}

async function tryDiscoverApiEndpoints(html: string, pageUrl: string, res: Response): Promise<{ url: string; name: string }[] | null> {
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
        sendSSE(res, "status", { message: `Trying hidden API: ${apiPath}...`, phase: "discover" });
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
          sendSSE(res, "status", { message: `API returned ${courses.length} courses on first page. Checking for more...`, phase: "discover" });

          const totalPages = data?.result?.totalPage ?? data?.totalPage ?? data?.totalPages ?? 1;
          const pageSize = data?.result?.pageSize ?? data?.pageSize ?? 20;

          if (totalPages > 1) {
            for (let page = 1; page < totalPages; page++) {
              try {
                const pageUrlObj = new URL(tryUrl);
                pageUrlObj.searchParams.set("pageQ", String(page));
                if (!pageUrlObj.searchParams.has("PageId")) {
                  const origParams = new URL(pageUrl).searchParams;
                  const pageId = origParams.get("PageId");
                  if (pageId) pageUrlObj.searchParams.set("PageId", pageId);
                }

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
                  const moreCourses = extractCoursesFromApiResponse(pData, origin);
                  courses.push(...moreCourses);
                  sendSSE(res, "status", { message: `Fetched page ${page + 1}/${totalPages} (${courses.length} total courses)`, phase: "discover" });
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
      if (obj.length > 0 && obj[0]?.header && obj[0]?.link) return obj;
      if (obj.length > 0 && (obj[0]?.name || obj[0]?.title || obj[0]?.courseName)) return obj;
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
    let url = "";
    if (item.link?.href) url = item.link.href;
    else if (item.url) url = item.url;
    else if (item.href) url = item.href;
    else if (item.link?.url) url = item.link.url;

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

router.post("/scrape/start", async (req: Request, res: Response): Promise<void> => {
  const { url, universityId, universityName, universityCountry, universityCity } = req.body as {
    url: string;
    universityId?: number;
    universityName?: string;
    universityCountry?: string;
    universityCity?: string;
  };

  if (!url) {
    res.status(400).json({ error: "URL is required" });
    return;
  }
  if (!GEMINI_API_KEY) {
    res.status(500).json({ error: "GEMINI_API_KEY not configured" });
    return;
  }

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.flushHeaders();

  try {
    let uniId: number;
    if (universityId) {
      const u = await db.select().from(universitiesTable).where(eq(universitiesTable.id, universityId));
      if (!u[0]) { sendSSE(res, "error", { message: "University not found" }); res.end(); return; }
      uniId = u[0].id;
      sendSSE(res, "status", { message: `Using existing university: ${u[0].name}` });
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
      sendSSE(res, "status", { message: `University: ${universityName} (ID: ${uniId})` });
    } else {
      sendSSE(res, "error", { message: "University ID or name is required" });
      res.end();
      return;
    }

    sendSSE(res, "status", { message: `Fetching ${url}...`, phase: "fetch" });

    let html: string;
    try {
      html = await fetchPage(url);
    } catch (err) {
      sendSSE(res, "error", { message: `Failed to fetch URL: ${(err as Error).message}` });
      res.end();
      return;
    }

    sendSSE(res, "status", { message: "Analyzing page with Gemini AI...", phase: "analyze" });
    const pageContent = extractPageContent(html, url);
    const analysis = await analyzePage(pageContent);

    if (analysis.pageType === "unknown") {
      sendSSE(res, "status", { message: "No course data in static HTML. Trying to discover hidden API endpoints...", phase: "discover" });

      const apiCourses = await tryDiscoverApiEndpoints(html, url, res);
      if (apiCourses && apiCourses.length > 0) {
        sendSSE(res, "status", {
          message: `Found ${apiCourses.length} courses via hidden API. Extracting details...`,
          phase: "extract",
          totalCourses: apiCourses.length,
        });

        let imported = 0, skipped = 0, errors = 0;
        const max = Math.min(apiCourses.length, 300);

        for (let i = 0; i < max; i++) {
          const link = apiCourses[i];
          sendSSE(res, "progress", { current: i + 1, total: max, courseName: link.name, message: `Scraping ${i + 1}/${max}: ${link.name}` });
          try {
            const cHtml = await fetchPage(link.url);
            const cContent = extractPageContent(cHtml, link.url);
            const cData = await extractCourseFromPage(cContent, link.name);
            if (cData) {
              cData.courseWebsite = cData.courseWebsite || link.url;
              const saved = await saveCourse(cData, uniId);
              if (saved) { imported++; sendSSE(res, "course", { name: cData.courseName, status: "imported", index: i + 1 }); }
              else { skipped++; sendSSE(res, "course", { name: cData.courseName, status: "skipped", index: i + 1 }); }
            } else {
              errors++;
              sendSSE(res, "course", { name: link.name, status: "error", message: "Could not extract course details", index: i + 1 });
            }
          } catch (err) {
            errors++;
            sendSSE(res, "course", { name: link.name, status: "error", message: (err as Error).message, index: i + 1 });
          }
          if (i % 5 === 4) await new Promise((r) => setTimeout(r, 500));
        }

        sendSSE(res, "done", { totalFound: apiCourses.length, imported, skipped, errors });
        res.end();
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
        sendSSE(res, "status", {
          message: `Found ${courseLinks.length} potential course links. Extracting data...`,
          phase: "extract",
          totalCourses: courseLinks.length,
        });

        let imported = 0, skipped = 0, errors = 0;
        const max = Math.min(courseLinks.length, 100);

        for (let i = 0; i < max; i++) {
          const link = courseLinks[i];
          sendSSE(res, "progress", { current: i + 1, total: max, courseName: link.name, message: `Scraping ${i + 1}/${max}: ${link.name}` });
          try {
            const cHtml = await fetchPage(link.url);
            const cContent = extractPageContent(cHtml, link.url);
            const cData = await extractCourseFromPage(cContent, link.name);
            if (cData) {
              cData.courseWebsite = cData.courseWebsite || link.url;
              const saved = await saveCourse(cData, uniId);
              if (saved) { imported++; sendSSE(res, "course", { name: cData.courseName, status: "imported", index: i + 1 }); }
              else { skipped++; sendSSE(res, "course", { name: cData.courseName, status: "skipped", index: i + 1 }); }
            } else {
              errors++;
              sendSSE(res, "course", { name: link.name, status: "error", message: "Not a course page", index: i + 1 });
            }
          } catch (err) {
            errors++;
            sendSSE(res, "course", { name: link.name, status: "error", message: (err as Error).message, index: i + 1 });
          }
          await new Promise((r) => setTimeout(r, 300));
        }

        sendSSE(res, "done", { totalFound: courseLinks.length, imported, skipped, errors });
        res.end();
        return;
      }

      sendSSE(res, "error", { message: "This page uses JavaScript to load courses dynamically. Try pasting a direct course page URL instead (e.g. https://university.edu/study/course/bachelor-of-business)." });
      res.end();
      return;
    }

    if (analysis.pageType === "detail" && analysis.courseData) {
      sendSSE(res, "status", { message: "Found single course page. Extracting data...", phase: "extract" });
      analysis.courseData.courseWebsite = analysis.courseData.courseWebsite || url;
      const saved = await saveCourse(analysis.courseData, uniId);
      sendSSE(res, "course", {
        name: analysis.courseData.courseName,
        status: saved ? "imported" : "skipped (duplicate)",
      });
      sendSSE(res, "done", { totalFound: 1, imported: saved ? 1 : 0, skipped: saved ? 0 : 1, errors: 0 });
      res.end();
      return;
    }

    if (analysis.pageType === "listing" && analysis.courseLinks?.length) {
      const courseLinks = analysis.courseLinks.filter((l) => l.url && l.name);
      sendSSE(res, "status", {
        message: `Found ${courseLinks.length} courses on listing page. Starting extraction...`,
        phase: "extract",
        totalCourses: courseLinks.length,
      });

      let imported = 0;
      let skipped = 0;
      let errors = 0;
      const maxCourses = Math.min(courseLinks.length, 100);

      for (let i = 0; i < maxCourses; i++) {
        const link = courseLinks[i];
        sendSSE(res, "progress", {
          current: i + 1,
          total: maxCourses,
          courseName: link.name,
          message: `Scraping ${i + 1}/${maxCourses}: ${link.name}`,
        });

        try {
          const courseHtml = await fetchPage(link.url);
          const courseContent = extractPageContent(courseHtml, link.url);
          const courseData = await extractCourseFromPage(courseContent, link.name);

          if (courseData) {
            courseData.courseWebsite = courseData.courseWebsite || link.url;
            const saved = await saveCourse(courseData, uniId);
            if (saved) {
              imported++;
              sendSSE(res, "course", { name: courseData.courseName, status: "imported", index: i + 1 });
            } else {
              skipped++;
              sendSSE(res, "course", { name: courseData.courseName, status: "skipped", index: i + 1 });
            }
          } else {
            errors++;
            sendSSE(res, "course", { name: link.name, status: "error", message: "Could not extract data", index: i + 1 });
          }
        } catch (err) {
          errors++;
          sendSSE(res, "course", { name: link.name, status: "error", message: (err as Error).message, index: i + 1 });
        }

        await new Promise((r) => setTimeout(r, 300));
      }

      sendSSE(res, "done", { totalFound: courseLinks.length, imported, skipped, errors });
      res.end();
      return;
    }

    sendSSE(res, "error", { message: "Could not extract any course data from this page." });
    res.end();
  } catch (err) {
    sendSSE(res, "error", { message: `Scraping failed: ${(err as Error).message}` });
    res.end();
  }
});

router.post("/scrape/preview", async (req: Request, res: Response): Promise<void> => {
  const { url } = req.body as { url: string };
  if (!url) { res.status(400).json({ error: "URL is required" }); return; }
  if (!GEMINI_API_KEY) { res.status(500).json({ error: "GEMINI_API_KEY not configured" }); return; }

  try {
    const html = await fetchPage(url);
    const content = extractPageContent(html, url);
    const analysis = await analyzePage(content);
    res.json(analysis);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

export default router;
