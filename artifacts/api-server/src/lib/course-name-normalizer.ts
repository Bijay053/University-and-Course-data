/**
 * Course name normalization helpers.
 *
 * Production bug (2026-04-23): 148 of 184 CSU courses were stored as
 * "Bachelor Business Studies" instead of "Bachelor of Business Studies"
 * because the link extraction / page-title cleanup pipeline silently dropped
 * the preposition.  The actual page title at
 *   https://study.csu.edu.au/international/courses/bachelor-business-studies
 * is "Bachelor of Business Studies", and the slug
 *   bachelor-of-business-studies
 * also makes that explicit on URLs where the title was lost.
 *
 * `validateNameAgainstSlug` reconstructs the missing preposition by
 * comparing the extracted name against the URL slug.  It is intentionally
 * conservative: only when the slug clearly contains a preposition that the
 * name lacks does it rebuild from the slug.
 *
 * Follow-up (2026-04-23): the page-title cleanup pipeline was also
 * blanket-title-casing names, producing "Mba Finance", "Bbus Marketing",
 * "Bachelor Of Business Studies", "Gdba - Graduate Diploma Of Business
 * Administration".  `normalizeCourseNameCasing` restores conventional
 * capitalization: known acronyms stay uppercase, function words ("of",
 * "and", "in", "the", "for") stay lowercase when not at the start of the
 * title, and everything else is capitalized.
 */

const PREPOSITIONS = new Set(["of", "in", "and", "the", "for"]);

// Words that should stay lower-case when title-casing slugs.
const LOWERCASE_WORDS = new Set([...PREPOSITIONS]);

// Course-name acronyms that should keep their uppercase form.  Keep this
// list focused on terms that actually appear in Australian higher-ed
// course names and admissions criteria so we don't accidentally upper-case
// ordinary English words.
const ACRONYMS = new Set([
  "MBA",
  "BBA",
  "BBUS",
  "BCOM",
  "MCOM",
  "GDBA",
  "GCBA",
  "MBBS",
  "JD",
  "LLB",
  "LLM",
  "PHD",
  "IT",
  "ICT",
  "AI",
  "TESOL",
  "IELTS",
  "PTE",
  "TOEFL",
  "ATAR",
  "OSHC",
  "GPA",
  "VET",
  "TAFE",
]);

const DEGREE_HEAD_RE =
  /^(bachelor|master|doctor|diploma|graduate certificate|graduate diploma|undergraduate certificate|honours)\b/i;

function normalizeWordCasing(word: string, isFirst: boolean): string {
  // Pull off leading and trailing non-letters (parens, brackets, punctuation)
  // so we only re-case the alphabetic core.
  const match = word.match(/^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$/);
  if (!match) return word;
  const [, prefix, core, suffix] = match;
  const upper = core.toUpperCase();
  if (ACRONYMS.has(upper)) return `${prefix}${upper}${suffix}`;
  const lower = core.toLowerCase();
  if (!isFirst && LOWERCASE_WORDS.has(lower)) return `${prefix}${lower}${suffix}`;
  // Roman numerals up to 4 chars (II, III, IV, etc.).
  if (/^[ivxlc]+$/i.test(core) && core.length <= 4) return `${prefix}${upper}${suffix}`;
  return `${prefix}${core.charAt(0).toUpperCase()}${core.slice(1).toLowerCase()}${suffix}`;
}

/**
 * Re-case a course name so acronyms stay uppercase ("MBA Finance"),
 * function words stay lowercase mid-title ("Bachelor of Business Studies"),
 * and everything else is capitalized.  Whitespace and punctuation
 * (including " - " separators) are preserved verbatim.
 */
export function normalizeCourseNameCasing(name: string): string {
  if (!name) return name;
  // Split keeping separators so " - " and runs of whitespace round-trip.
  const tokens = name.split(/(\s+|-)/);
  let seenWord = false;
  return tokens
    .map((tok) => {
      if (!tok) return tok;
      if (/^\s+$/.test(tok) || tok === "-") return tok;
      const out = normalizeWordCasing(tok, !seenWord);
      seenWord = true;
      return out;
    })
    .join("");
}

function rebuildFromSlug(slug: string): string {
  const words = slug.split(/[-_]+/).filter(Boolean);
  return normalizeCourseNameCasing(words.join(" "));
}

function lastSlugSegment(url: string): string | null {
  try {
    const u = new URL(url);
    const parts = u.pathname.split("/").filter(Boolean);
    if (parts.length === 0) return null;
    let seg = parts[parts.length - 1];
    seg = seg.replace(/\.(html?|aspx?|php)$/i, "");
    return seg || null;
  } catch {
    return null;
  }
}

/**
 * If the URL slug clearly contains a preposition (of/in/and) that the
 * extracted course name is missing, prefer the slug-derived name.  In every
 * other case the extracted name is returned with normalized capitalization
 * (so upstream "Mba Finance" / "Bachelor Of Business Studies" still get
 * cleaned up even when the slug doesn't need to be consulted).
 */
export function validateNameAgainstSlug(extractedName: string, url: string | null | undefined): string {
  if (!extractedName) return extractedName;
  const normalized = normalizeCourseNameCasing(extractedName);
  if (!url) return normalized;
  const slug = lastSlugSegment(url);
  if (!slug) return normalized;

  const slugWords = slug.toLowerCase().split(/[-_]+/).filter(Boolean);
  const nameWords = extractedName.toLowerCase().match(/[a-z]+/g) ?? [];

  // Only act on canonical degree-style names — don't risk rewriting random
  // course-list page titles or marketing names.
  if (!DEGREE_HEAD_RE.test(extractedName)) return normalized;

  const slugPreps = slugWords.filter((w) => PREPOSITIONS.has(w));
  if (slugPreps.length === 0) return normalized;

  const namePreps = nameWords.filter((w) => PREPOSITIONS.has(w));
  if (namePreps.length >= slugPreps.length) return normalized;

  // Stem comparison: the non-preposition slug words should be roughly the
  // same set as the non-preposition name words.  Otherwise the slug is for
  // a different page (eg listing/category) and we shouldn't rewrite.
  const slugContent = new Set(slugWords.filter((w) => !PREPOSITIONS.has(w)));
  const nameContent = new Set(nameWords.filter((w) => !PREPOSITIONS.has(w)));
  let overlap = 0;
  for (const w of nameContent) if (slugContent.has(w)) overlap += 1;
  const overlapRatio = nameContent.size === 0 ? 0 : overlap / nameContent.size;
  if (overlapRatio < 0.6) return normalized;

  return rebuildFromSlug(slug);
}
