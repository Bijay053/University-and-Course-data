/**
 * Course-location validation.
 *
 * Production bug (2026-04-23): 57 courses had garbage in `course_location`:
 *   - "test" (placeholder)
 *   - "On Campus"     (study mode, not a location)
 *   - "If you're having trouble accessing Google Search, please click here..."
 *   - "Port Macquarie, Jilin Uni - Finance & Economics, ..."
 *   - "Session 1 : March 2"
 *   - "SPACE University of Hong Kong"
 *
 * The existing `sanitizeCourseLocationForDisplay` only strips online/virtual
 * tokens; it has no concept of "is this even a place name?".  This validator
 * adds a hard blocklist + a campus whitelist so obvious junk is rejected
 * before being persisted.
 */

const VALID_AU_CAMPUSES = new Set([
  // capital + major cities
  "sydney", "melbourne", "brisbane", "perth", "adelaide",
  "hobart", "canberra", "darwin", "gold coast", "newcastle",
  "wollongong", "geelong", "cairns", "townsville", "ballarat",
  "bendigo", "launceston", "rockhampton", "mackay",
  // CSU
  "bathurst", "albury-wodonga", "albury", "wodonga", "dubbo",
  "port macquarie", "wagga wagga", "orange",
  // USQ
  "toowoomba", "springfield", "ipswich",
  // Common other campus / mode tokens we *do* want to keep
  "holmesglen", "external", "online", "uni wide",
  "parramatta", "liverpool", "bankstown", "macarthur", "campbelltown",
  "north sydney", "north ryde", "broadway", "ultimo", "lismore",
  "armidale", "tamworth", "bundaberg", "fraser coast", "sunshine coast",
]);

const LOCATION_BLOCKLIST_PATTERNS: RegExp[] = [
  /click here/i,
  /\bgoogle\b/i,
  /feedback/i,
  /trouble accessing/i,
  /\bsession\s*\d/i,
  /^test$/i,
  /^on campus$/i,
  // Exchange-partner artefacts seen in CSU "course_location"
  /\bjilin\b|\btianjin\b|\byangzhou\b|\byunnan\b/i,
  /\bSPACE\b.*\buniversity\b/i,
  /university of (hong kong|tokyo|singapore|malaya|auckland)/i,
];

/**
 * Returns the cleaned location string, or `null` if the value is not a
 * legitimate location.  Idempotent.
 */
export function validateCourseLocation(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (trimmed.length > 150) return null; // prose, not a location

  // Split on comma first so that one bad partner-uni name doesn't poison
  // the whole field (e.g. "Port Macquarie, Jilin Uni - Finance" should
  // keep Port Macquarie, not return null because the string contains
  // "jilin").
  const parts = trimmed.split(",").map((p) => p.trim()).filter(Boolean);
  const kept: string[] = [];
  for (const part of parts) {
    const lower = part.toLowerCase();

    // Per-part blocklist
    let blocked = false;
    for (const pat of LOCATION_BLOCKLIST_PATTERNS) {
      if (pat.test(part)) { blocked = true; break; }
    }
    if (blocked) continue;

    if (VALID_AU_CAMPUSES.has(lower)) {
      kept.push(part);
      continue;
    }
    // Generic place-shaped token: 2-30 chars, letters/spaces/hyphens only.
    if (lower.length >= 2 && lower.length <= 30 && /^[a-z][a-z\s\-']+$/i.test(lower)) {
      kept.push(part);
    }
  }

  if (kept.length === 0) return null;
  return kept.join(", ");
}
