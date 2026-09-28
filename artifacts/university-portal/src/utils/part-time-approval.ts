type StudyFields = {
  studyLoad?: string | null;
  study_load?: string | null;
  studyMode?: string | null;
  study_mode?: string | null;
};

export const PART_TIME_APPROVAL_REASON = "Part-time-only courses cannot be published.";

function loadKind(value: string | null | undefined): "part" | "mixed" | "other" | "unknown" {
  if (!value?.trim()) return "unknown";
  const normalized = value.toLowerCase().replace(/[_-]/g, " ").replace(/\s+/g, " ").trim();
  const hasPart = /\bpart\s*time(?:\s*only)?\b/.test(normalized) || /\bparttime(?:only)?\b/.test(normalized);
  const hasFull = /\bfull\s*time\b/.test(normalized) || /\bfulltime\b/.test(normalized);
  return hasPart && hasFull ? "mixed" : hasPart ? "part" : "other";
}

/** A legacy PT-only mode takes priority over a defaulted FullTime load. */
export function isPartTimeOnlyCourse(course: StudyFields): boolean {
  const loadValue = course.studyLoad ?? course.study_load;
  const modeValue = course.studyMode ?? course.study_mode;
  const explicitOnly = /\bpart[\s_-]*time\s*only\b|\bonly\s+(?:available|offered|delivered|studied)\s+(?:as\s+|on\s+a\s+)?part[\s_-]*time\b/i;
  if ([loadValue, modeValue].some(value => value && explicitOnly.test(value))) return true;
  const load = loadKind(loadValue);
  const mode = loadKind(modeValue);
  if (modeValue?.trim().toLowerCase() === "both" || mode === "mixed") return false;
  if (mode === "part") return true;
  if (load === "mixed" || /\bfull[\s_-]*time\b/i.test(modeValue ?? "")) return false;
  return load === "part";
}

export function blockedApprovalIds<T extends StudyFields & { id: number }>(
  courses: T[], ids: number[],
): number[] {
  const requested = new Set(ids);
  return courses.filter(course => requested.has(course.id) && isPartTimeOnlyCourse(course)).map(course => course.id);
}