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
 */

const PREPOSITIONS = new Set(["of", "in", "and", "the", "for"]);

// Words that should stay lower-case when title-casing slugs.
const LOWERCASE_WORDS = new Set([...PREPOSITIONS]);

const DEGREE_HEAD_RE =
  /^(bachelor|master|doctor|diploma|graduate certificate|graduate diploma|undergraduate certificate|honours)\b/i;

function titleCaseSlugWord(word: string, isFirst: boolean): string {
  const lower = word.toLowerCase();
  if (!isFirst && LOWERCASE_WORDS.has(lower)) return lower;
  if (/^[ivxlc]+$/i.test(word) && word.length <= 4) return word.toUpperCase(); // Roman numerals
  return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
}

function rebuildFromSlug(slug: string): string {
  const words = slug.split(/[-_]+/).filter(Boolean);
  return words.map((w, i) => titleCaseSlugWord(w, i === 0)).join(" ");
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
 * extracted course name is missing, prefer the slug-derived name. Returns
 * the original name in every other case so we never *introduce* errors.
 */
export function validateNameAgainstSlug(extractedName: string, url: string | null | undefined): string {
  if (!extractedName || !url) return extractedName;
  const slug = lastSlugSegment(url);
  if (!slug) return extractedName;

  const slugWords = slug.toLowerCase().split(/[-_]+/).filter(Boolean);
  const nameWords = extractedName.toLowerCase().match(/[a-z]+/g) ?? [];

  // Only act on canonical degree-style names — don't risk rewriting random
  // course-list page titles or marketing names.
  if (!DEGREE_HEAD_RE.test(extractedName)) return extractedName;

  const slugPreps = slugWords.filter((w) => PREPOSITIONS.has(w));
  if (slugPreps.length === 0) return extractedName;

  const namePreps = nameWords.filter((w) => PREPOSITIONS.has(w));
  if (namePreps.length >= slugPreps.length) return extractedName;

  // Stem comparison: the non-preposition slug words should be roughly the
  // same set as the non-preposition name words.  Otherwise the slug is for
  // a different page (eg listing/category) and we shouldn't rewrite.
  const slugContent = new Set(slugWords.filter((w) => !PREPOSITIONS.has(w)));
  const nameContent = new Set(nameWords.filter((w) => !PREPOSITIONS.has(w)));
  let overlap = 0;
  for (const w of nameContent) if (slugContent.has(w)) overlap += 1;
  const overlapRatio = nameContent.size === 0 ? 0 : overlap / nameContent.size;
  if (overlapRatio < 0.6) return extractedName;

  return rebuildFromSlug(slug);
}
