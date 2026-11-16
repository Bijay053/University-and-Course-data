import type { Course } from "@workspace/api-client-react";
import { PublishedIntakeFees } from "./published-intake-fees";

export function formatCampusTuition(offering: NonNullable<Course["offerings"]>[number]): string {
  if (offering.feeAmount == null) return "Fee not published for this location";
  return `${offering.feeCurrency || "Currency not published"} ${offering.feeAmount.toLocaleString("en-GB")} / ${offering.feeTerm || "Term not published"} · ${offering.feeYear ?? "Year not published"}`;
}

/** Spreadsheet tuition columns follow the same offering authority as the UI. */
export function courseFeeExportColumns(course: Record<string, unknown>) {
  const offerings = course.offerings as Course["offerings"];
  const hasOfferings = !!offerings?.length;
  const schedules = course.feeSchedules as Course["feeSchedules"];
  if (schedules?.length || course.feeScheduleReviewRequired) return {
    "Campus fees": schedules?.map(s => `${s.intakes.join(", ")} ${s.entryYear}: ${s.currency} ${s.amount.toLocaleString()} / ${s.basis} · fee year ${s.feeYear}`).join("\n")
      || "Intake-linked tuition requires review",
    "Int'l Fee": "", "Fee Term": "", "Fee Year": "", "Currency": "",
  };
  return {
    "Campus fees": hasOfferings
      ? offerings.map(o => `${o.location}: ${formatCampusTuition(o)}`).join("\n")
      : "",
    "Int'l Fee": !hasOfferings && course.internationalFee != null ? Number(course.internationalFee) : "",
    "Fee Term": !hasOfferings ? String(course.feeTerm ?? "") : "",
    "Fee Year": !hasOfferings && course.feeYear != null ? Number(course.feeYear) : "",
    "Currency": !hasOfferings ? String(course.feeCurrency ?? course.currency ?? "") : "",
  };
}

/** Never collapse prices across campuses, years, currencies or fee periods. */
export function CourseCampusFees({ course }: { course: Pick<Course, "offerings" | "internationalFee" | "currency" | "feeTerm" | "feeYear" | "feeSchedules" | "feeScheduleReviewRequired"> }) {
  if (course.feeSchedules?.length || course.feeScheduleReviewRequired) {
    return <PublishedIntakeFees schedules={course.feeSchedules ?? []} reviewRequired={course.feeScheduleReviewRequired} />;
  }
  if (course.offerings?.length) {
    return (
      <ul className="space-y-1 min-w-48" data-testid="list-campus-fees">
        {course.offerings.map((o) => (
          <li key={o.id} data-testid={`text-campus-fee-${o.id}`}>
            <strong>{o.location}:</strong>{" "}
            <span>{formatCampusTuition(o)}</span>
          </li>
        ))}
      </ul>
    );
  }
  return <span data-testid="text-legacy-fee">{course.internationalFee != null
    ? `${course.currency || ""} ${course.internationalFee.toLocaleString("en-GB")} / ${course.feeTerm || "Term not published"} · ${course.feeYear ?? "Year not published"}`
    : "—"}</span>;
}