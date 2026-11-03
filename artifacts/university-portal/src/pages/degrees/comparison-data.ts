import { type CompareCourse, NOT_PUBLISHED, feeLines, formatDuration, formatFeeLine, safeHttpUrl } from "./lib";

export function comparisonRows(courses: CompareCourse[]) {
  const definitions: [string, (c: CompareCourse) => string][] = [
    ["University", (c) => c.university?.name || NOT_PUBLISHED],
    ["Level", (c) => c.degree_level || NOT_PUBLISHED],
    ["Location", (c) => c.course_location || [c.university?.city, c.university?.country].filter(Boolean).join(", ") || NOT_PUBLISHED],
    ["Duration", (c) => formatDuration(c.duration, c.duration_term)],
    ["Study mode", (c) => c.study_mode || NOT_PUBLISHED],
    ["Intakes", (c) => c.intakes?.length ? c.intakes.join(", ") : NOT_PUBLISHED],
    ["Fees (as published)", (c) => feeLines(c).map((f) => `${f.label && (c.offerings?.length ?? 0) > 1 ? `${f.label}: ` : ""}${formatFeeLine(f)}`).join("\n")],
    ["English", (c) => c.english_requirements?.length ? c.english_requirements.map((e) => `${e.test_type || e.test_name || "Test"} ${e.overall ?? NOT_PUBLISHED}`).join("\n") : NOT_PUBLISHED],
    ["Academic entry", (c) => c.academic_requirements?.length ? c.academic_requirements.map((a) => [a.academic_level, a.academic_score != null ? `${a.academic_score}${a.score_type ? ` ${a.score_type}` : ""}` : null].filter(Boolean).join(", ") || NOT_PUBLISHED).join("\n") : NOT_PUBLISHED],
    ["Official page", (c) => safeHttpUrl(c.course_url) || NOT_PUBLISHED],
  ];
  return definitions.map(([label, value]) => ({ label, values: courses.map(value) }));
}
