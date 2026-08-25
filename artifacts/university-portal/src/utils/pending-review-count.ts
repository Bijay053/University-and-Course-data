type StagedCourseStatus = { status?: unknown };

/**
 * Count the rows that the Review screen can actually display.
 *
 * The current API envelope contains only pending rows, but older responses can
 * be a bare array. Filter by status in either shape so the completion card and
 * Review table always describe the same set of courses.
 */
export function countPendingReviewCourses(payload: unknown): number | null {
  const rows: unknown[] | null = Array.isArray(payload)
    ? payload
    : payload !== null
      && typeof payload === "object"
      && Array.isArray((payload as { courses?: unknown }).courses)
        ? (payload as { courses: unknown[] }).courses
        : null;

  if (rows === null) return null;

  return rows.filter((row) =>
    row !== null
    && typeof row === "object"
    && (row as StagedCourseStatus).status === "pending",
  ).length;
}