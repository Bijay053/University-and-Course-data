import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";

export type TargetedRetryDiagnostic = {
  error_type: string;
  message?: string;
  selected_count?: number;
  filtered_count?: number;
  processed_count?: number;
  selected_urls?: string[];
  source_review_job_id?: string | null;
  retry_source_job_id?: string | null;
  recovery_action?: string;
};

export function isTargetedRetryAllFiltered(
  diagnostic: TargetedRetryDiagnostic | null | undefined,
): diagnostic is TargetedRetryDiagnostic {
  return diagnostic?.error_type === "targeted_retry_all_filtered";
}

export function TargetedRetryAllFilteredNotice({
  diagnostic,
  diagnosticId,
  onReportOfficialUrls,
}: {
  diagnostic: TargetedRetryDiagnostic | null | undefined;
  diagnosticId: string;
  onReportOfficialUrls: () => void;
}) {
  if (!isTargetedRetryAllFiltered(diagnostic)) return null;

  const filteredCount = diagnostic.filtered_count;
  const alreadyResolvedCount = (
    typeof diagnostic.selected_count === "number"
    && typeof filteredCount === "number"
    && diagnostic.selected_count > filteredCount
  )
    ? diagnostic.selected_count - filteredCount
    : null;

  return (
    <div
      className="w-full space-y-2 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-900"
      role="alert"
      data-testid={`status-targeted-retry-all-filtered-${diagnosticId}`}
    >
      <p className="flex items-start gap-2 font-semibold">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <span>No remaining selected course URLs were processed.</span>
      </p>
      <p>
        {alreadyResolvedCount !== null && (
          <>
            {alreadyResolvedCount} of the {diagnostic.selected_count} selected course URLs{" "}
            {alreadyResolvedCount === 1 ? "was" : "were"} already resolved.{" "}
          </>
        )}
        URL filters prevented {filteredCount ?? "the"} remaining unresolved selected course URL
        {filteredCount === 1 ? "" : "s"} from being processed. Existing review records were not
        changed.
      </p>
      <p>
        Submit the official course URLs below to start the established bounded recovery. Eligibility
        and staging safeguards remain in place.
      </p>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="border-red-300 bg-white text-red-900 hover:bg-red-100"
        onClick={onReportOfficialUrls}
        data-testid={`button-report-filtered-course-urls-${diagnosticId}`}
      >
        Report official course URLs
      </Button>
    </div>
  );
}