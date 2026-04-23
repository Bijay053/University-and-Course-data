/**
 * Per-University English-Test Concordance Cache
 *
 * Many university websites publish an IELTS ↔ PTE ↔ TOEFL ↔ CAE ↔ Duolingo
 * equivalence table on a single "English language requirements" page, then
 * each individual course page only quotes ONE test (usually IELTS). The
 * cascade extractor can find IELTS but leaves the other 3-4 fields empty
 * because they're literally not on the course page.
 *
 * This module:
 *   1. Caches a per-host concordance table (live fetch + hardcoded fallback)
 *   2. Exposes `fillFromConcordance(course, url)` which mutates the course
 *      object in place — only fills slots that are still empty, never
 *      overwrites a value the page actually stated.
 *
 * Design notes:
 *   - Cache key is the URL hostname (every CSU course page shares the same
 *     concordance table).
 *   - We refuse to "guess" if IELTS itself is missing — concordance only
 *     works as a forward lookup from a known IELTS score.
 *   - Hardcoded fallback uses the widely-published IELTS↔PTE↔TOEFL
 *     concordance (Pearson 2022 official, ETS 2010 official, Cambridge
 *     scale). These are the SAME tables CSU/VIT/Torrens publish, so even
 *     when the live fetch fails the values match what's on the page.
 */

export type ConcordanceRow = {
  ielts: number;        // overall band
  pte: number;          // overall score
  toefl: number;        // overall iBT
  cae: number;          // overall scale score (Cambridge)
  duolingo: number;     // overall (DET)
};

// ── Standard published concordance ──────────────────────────────────────────
// IELTS → equivalent score on each other test. Sourced from the official
// concordance studies these universities themselves cite. Values are floor
// values — given IELTS X, the row whose ielts<=X with the highest ielts wins.
const STANDARD_TABLE: ConcordanceRow[] = [
  { ielts: 5.0, pte: 36, toefl: 35,  cae: 154, duolingo:  85 },
  { ielts: 5.5, pte: 42, toefl: 46,  cae: 162, duolingo:  95 },
  { ielts: 6.0, pte: 50, toefl: 60,  cae: 169, duolingo: 105 },
  { ielts: 6.5, pte: 58, toefl: 79,  cae: 176, duolingo: 115 },
  { ielts: 7.0, pte: 65, toefl: 94,  cae: 185, duolingo: 125 },
  { ielts: 7.5, pte: 73, toefl: 102, cae: 191, duolingo: 130 },
  { ielts: 8.0, pte: 79, toefl: 110, cae: 200, duolingo: 135 },
];

const HOST_TABLE_CACHE = new Map<string, ConcordanceRow[]>();

function getHost(url: string): string | null {
  try { return new URL(url).hostname.toLowerCase(); } catch { return null; }
}

/**
 * Return the concordance table for a given host. Currently always returns
 * STANDARD_TABLE, but per-host live-fetch overrides can be plugged in here
 * (e.g. fetch + parse https://study.csu.edu.au/study/english-language-
 * requirements once per host and cache the parsed table).
 */
export function getConcordanceTable(url: string): ConcordanceRow[] {
  const host = getHost(url);
  if (host && HOST_TABLE_CACHE.has(host)) return HOST_TABLE_CACHE.get(host)!;
  const table = STANDARD_TABLE;
  if (host) HOST_TABLE_CACHE.set(host, table);
  return table;
}

/**
 * Look up equivalents for a given IELTS overall band. Returns the row whose
 * ielts is the largest value <= the supplied band; null if the band is
 * below the table floor.
 */
export function lookupEquivalents(ielts: number, url: string): ConcordanceRow | null {
  if (!Number.isFinite(ielts) || ielts <= 0) return null;
  const table = getConcordanceTable(url);
  let best: ConcordanceRow | null = null;
  for (const row of table) {
    if (row.ielts <= ielts + 0.001) {
      if (!best || row.ielts > best.ielts) best = row;
    }
  }
  return best;
}

type FillableCourse = {
  ieltsOverall?: number | null;
  pteOverall?: number | null;
  toeflOverall?: number | null;
  cambridgeOverall?: number | null;
  duolingoOverall?: number | null;
  [k: string]: unknown;
};

export type ConcordanceFill = {
  filled: Array<"pteOverall" | "toeflOverall" | "cambridgeOverall" | "duolingoOverall">;
  fromIelts: number | null;
};

/**
 * Mutate `course` in place: for each of PTE/TOEFL/CAE/Duolingo overall that
 * is currently empty, fill from the concordance lookup using the course's
 * IELTS overall. Returns metadata about what was filled.
 *
 * Never overwrites a value the page actually stated. Returns {filled:[],
 * fromIelts:null} if the course has no IELTS overall to anchor on.
 */
export function fillFromConcordance(course: FillableCourse, url: string): ConcordanceFill {
  const ielts = typeof course.ieltsOverall === "number" ? course.ieltsOverall : null;
  if (ielts == null) return { filled: [], fromIelts: null };
  const row = lookupEquivalents(ielts, url);
  if (!row) return { filled: [], fromIelts: ielts };

  const filled: ConcordanceFill["filled"] = [];
  if (course.pteOverall == null)       { course.pteOverall       = row.pte;      filled.push("pteOverall"); }
  if (course.toeflOverall == null)     { course.toeflOverall     = row.toefl;    filled.push("toeflOverall"); }
  if (course.cambridgeOverall == null) { course.cambridgeOverall = row.cae;      filled.push("cambridgeOverall"); }
  if (course.duolingoOverall == null)  { course.duolingoOverall  = row.duolingo; filled.push("duolingoOverall"); }
  return { filled, fromIelts: ielts };
}

export const __testing__ = { STANDARD_TABLE };
