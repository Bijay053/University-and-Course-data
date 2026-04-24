import { isCsuCoursePage } from "./csu-campus-fallback.ts";

/**
 * The structural shape `needsBrowserFallback` reads from `extractWithCheerio`'s
 * return value. Declared locally so the helper is unit-testable without
 * pulling in the full `routes/scrape.ts` dependency graph.
 */
export type BrowserFallbackInput = {
  courseName?: string | null;
  ieltsOverall?: number | null;
  pteOverall?: number | null;
  toeflOverall?: number | null;
  internationalFee?: number | null;
  duration?: string | null;
  degreeLevel?: string | null;
  courseLocation?: string | null;
  intakeMonths?: string[] | null;
  studyMode?: string | null;
};

/**
 * Decide whether a static (cheerio) extraction was incomplete enough that we
 * should pay for a Playwright browser fetch as a second pass.
 *
 * The `url` argument enables host-specific escalation rules — most notably
 * the CSU rule, which fires when a study.csu.edu.au course page came back
 * without a campus list (CSU hydrates the offerings JSON client-side).
 */
export function needsBrowserFallback(
  data: BrowserFallbackInput,
  url?: string,
): boolean {
  // No course name at all → likely fully JS-rendered, worth trying browser.
  if (!data.courseName) return true;
  const hasEnglish = !!(data.ieltsOverall || data.pteOverall || data.toeflOverall);
  // If no English test found at all, ALWAYS try browser — the requirement
  // block is almost certainly behind a JS-rendered tab / accordion (e.g.
  // ASA, VU, UEL).
  if (!hasEnglish) return true;
  const hasFee = !!data.internationalFee;
  const hasDuration = !!data.duration;
  const hasDegree = !!data.degreeLevel;
  const hasLocation = !!(data.courseLocation && data.courseLocation.trim().length > 2);
  const hasIntakes = !!data.intakeMonths?.length;
  if (data.studyMode !== "Online" && !hasLocation && !hasIntakes) return true;
  // CSU-specific: study.csu.edu.au hydrates the campus list
  // (`ocb_metadata.course_offerings`) client-side. Even when the static page
  // yields English/fee/duration, the campus chips can be empty — and the
  // textual fallback in csu-campus-fallback.ts cannot recover them when the
  // heading itself is rendered by JS. Escalate to a browser fetch (which
  // will wait for the offerings JSON) whenever the CSU course page came
  // back without a courseLocation.
  if (url && isCsuCoursePage(url) && !hasLocation && data.studyMode !== "Online") return true;
  // Two or more key fields found → static extraction is working; no browser needed.
  return [hasFee, hasEnglish, hasDuration, hasDegree].filter(Boolean).length < 2;
}
