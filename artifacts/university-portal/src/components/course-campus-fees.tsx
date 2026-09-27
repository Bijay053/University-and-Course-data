import type { Course } from "@workspace/api-client-react";

/** Never collapse prices across campuses, years, currencies or fee periods. */
export function CourseCampusFees({ course }: { course: Pick<Course, "offerings" | "internationalFee" | "currency" | "feeTerm" | "feeYear"> }) {
  if (course.offerings?.length) {
    return (
      <ul className="space-y-1 min-w-48" data-testid="list-campus-fees">
        {course.offerings.map((o) => (
          <li key={o.id} data-testid={`text-campus-fee-${o.id}`}>
            <strong>{o.location}:</strong>{" "}
            {o.feeAmount == null ? "Fee not published for this location" : (
              <span>
                {o.feeCurrency || "Currency not published"} {o.feeAmount.toLocaleString("en-GB")}
                {" / "}{o.feeTerm || "Term not published"}
                {" · "}{o.feeYear ?? "Year not published"}
              </span>
            )}
          </li>
        ))}
      </ul>
    );
  }
  return <span data-testid="text-legacy-fee">{course.internationalFee != null
    ? `${course.currency || ""} ${course.internationalFee.toLocaleString("en-GB")} / ${course.feeTerm || "Term not published"} · ${course.feeYear ?? "Year not published"}`
    : "—"}</span>;
}