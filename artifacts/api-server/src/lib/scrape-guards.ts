function normalize(input: string): string {
  return input.toLowerCase().replace(/[^a-z0-9\s]+/g, " ").replace(/\s+/g, " ").trim();
}

const GENERIC_CATEGORY_NAMES = new Set([
  "master s degrees",
  "masters degrees",
  "design",
  "business",
  "health",
  "hospitality",
  "technology",
  "education",
  "higher degrees by research",
  "higher degree by research",
  "research",
  "single subjects",
  "digital badges",
  "on demand short courses",
  "short courses",
]);

export function isGenericCourseCategoryName(name: string): boolean {
  const raw = name.trim();
  if (/^master'?s degrees?$/i.test(raw)) return true;
  if (/^graduate diploma$/i.test(raw)) return true;
  if (/^graduate certificate$/i.test(raw)) return true;
  const lower = normalize(name);
  if (!lower) return true;
  if (GENERIC_CATEGORY_NAMES.has(lower)) return true;
  if (/^(design|business|health|hospitality|technology|education)$/.test(lower)) return true;
  if (/^(single subjects?|digital badges?|on demand short courses?)$/.test(lower)) return true;
  return false;
}

function significantCourseTokens(courseName: string): string[] {
  return normalize(courseName)
    .split(" ")
    .filter((word) => word.length > 4)
    .filter((word) => !/^(bachelor|master|doctor|graduate|diploma|certificate|advanced|course|degree|program|online|studies|partnership|with)$/.test(word));
}

export function hasCourseSpecificFeeEvidence(courseName: string, searchText: string): boolean {
  const lowerText = normalize(searchText);
  const lowerCourse = normalize(courseName);
  if (lowerCourse.length >= 10 && lowerText.includes(lowerCourse)) return true;

  const tokens = significantCourseTokens(courseName);
  if (tokens.length === 0) return false;
  const matched = tokens.filter((token) => lowerText.includes(token)).length;
  return matched >= Math.min(2, tokens.length);
}

export function shouldTrustGenericUniversityFeeFallback(
  feePage: string,
  courseName: string,
  searchText: string,
  uniqueAmounts: number[],
): boolean {
  const feePageSlug = (() => {
    try {
      return new URL(feePage).pathname.toLowerCase();
    } catch {
      return "";
    }
  })();

  const tokens = significantCourseTokens(courseName);
  const feePageLooksCourseSpecific =
    tokens.length > 0 && tokens.some((token) => feePageSlug.includes(token));
  if (feePageLooksCourseSpecific) return true;

  if (uniqueAmounts.length !== 1) return false;

  const lowerText = searchText.toLowerCase();
  if (
    /\bfee-help\b|\bhelp loan\b|\bvet student loan\b|\bloan limit\b/.test(lowerText) &&
    !/\b(course fee|tuition fee|international course fee schedule|international tuition)\b/.test(lowerText)
  ) {
    return false;
  }

  return hasCourseSpecificFeeEvidence(courseName, searchText);
}
