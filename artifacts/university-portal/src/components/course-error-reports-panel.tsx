import { useState } from "react";
import { Link } from "wouter";
import { ExternalLink } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/context/auth";
import {
  REPORT_FIELDS, ReportApiError, type ReportStatus,
  useCourseErrorReports, useUpdateReportStatus,
} from "@/lib/course-error-reports";

const LIMIT = 20;
const FILTERS: (ReportStatus | "all")[] = ["new", "reviewed", "resolved", "all"];
const STATUS_STYLE: Record<ReportStatus, string> = {
  new: "bg-destructive/10 text-destructive",
  reviewed: "bg-amber-100 text-amber-800",
  resolved: "bg-emerald-100 text-emerald-800",
};
const fieldLabel = (f: string) => REPORT_FIELDS.find((x) => x.value === f)?.label ?? f;

function fmt(ts: string) {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function CourseErrorReportsPanel() {
  const { can } = useAuth();
  const canView = can("courses.view");
  const canEdit = can("courses.edit");
  const [status, setStatus] = useState<ReportStatus | "all">("new");
  const [offset, setOffset] = useState(0);
  const q = useCourseErrorReports(status, offset, canView, LIMIT);
  const update = useUpdateReportStatus();

  if (!canView) return null;

  const denied = q.error instanceof ReportApiError && q.error.status === 403;
  const total = q.data?.total ?? 0;

  return (
    <Card data-testid="panel-course-error-reports">
      <CardHeader className="flex flex-col gap-3 space-y-0 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <CardTitle className="text-base">Visitor error reports</CardTitle>
          <p className="text-sm text-muted-foreground">Reports never change course data. Verify, edit the course, then resolve.</p>
        </div>
        <div role="tablist" aria-label="Filter reports by status" className="inline-flex rounded-md border p-0.5 text-xs">
          {FILTERS.map((f) => (
            <button key={f} type="button" role="tab" aria-selected={status === f} onClick={() => { setStatus(f); setOffset(0); }}
              data-testid={`filter-reports-${f}`}
              className={`rounded px-2.5 py-1 font-medium capitalize ${status === f ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}>{f}</button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        {q.isLoading ? (
          <div className="space-y-2" aria-busy="true">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-16 w-full" />)}</div>
        ) : denied ? (
          <p className="text-sm text-muted-foreground" data-testid="text-reports-denied">You do not have access to error reports.</p>
        ) : q.isError ? (
          <div role="alert" className="flex items-center justify-between gap-3 rounded-md border border-destructive/30 p-3 text-sm" data-testid="error-reports">
            <span>Error reports could not be loaded.</span>
            <button type="button" onClick={() => q.refetch()} className="rounded-md border px-3 py-1 font-medium" data-testid="button-retry-reports">Retry</button>
          </div>
        ) : !q.data?.reports.length ? (
          <p className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground" data-testid="empty-reports">
            {status === "all" ? "No reports yet." : `No ${status} reports.`}
          </p>
        ) : (
          <>
            {update.isError && <p role="alert" className="mb-2 text-sm text-destructive" data-testid="error-report-update">{update.error.message}</p>}
            <ul className="divide-y rounded-md border">
              {q.data.reports.map((r) => {
                const busy = update.isPending && update.variables?.id === r.id;
                const universityHref = r.university_id ? `/universities/${r.university_id}` : null;
                const reportHref = r.field === "key_insights"
                  ? universityHref && `${universityHref}?tab=assessment`
                  : r.course_id ? `/courses/${r.course_id}` : universityHref;
                const title = r.field === "key_insights"
                  ? `Key Insights — ${r.insight_country || "General guidance"}`
                  : r.course_name ?? "Deleted course";
                const act = (s: ReportStatus, label: string) => (
                  <button type="button" disabled={busy} onClick={() => update.mutate({ id: r.id, status: s })}
                    data-testid={`button-report-${s}-${r.id}`}
                    className="h-8 rounded-md border px-2.5 text-xs font-medium hover:bg-muted disabled:opacity-50">{label}</button>
                );
                return (
                  <li key={r.id} className="space-y-1.5 p-3" data-testid={`row-report-${r.id}`}>
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="font-medium" data-testid={`text-report-course-${r.id}`}>
                          {reportHref ? (
                            <Link href={reportHref} className="text-primary underline decoration-primary/40 underline-offset-2 hover:decoration-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary" data-testid={`link-report-page-${r.id}`}>{title}</Link>
                          ) : title}
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {universityHref ? (
                            <Link href={universityHref} className="underline underline-offset-2 hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary" data-testid={`link-report-university-${r.id}`}>{r.university_name ?? "View university"}</Link>
                          ) : r.university_name ?? "Unknown university"}
                        </p>
                      </div>
                      <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold capitalize ${STATUS_STYLE[r.status] ?? ""}`} data-testid={`status-report-${r.id}`}>{r.status}</span>
                    </div>
                    <p className="text-xs">
                      <span className="font-semibold capitalize">{r.kind}</span> <span className="text-muted-foreground">information:</span> <span className="font-semibold">{fieldLabel(r.field)}</span>
                    </p>
                    <p className="whitespace-pre-wrap break-words text-sm" data-testid={`text-report-message-${r.id}`}>{r.message}</p>
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                      <div className="flex flex-wrap items-center gap-3">
                        <time dateTime={r.created_at}>{fmt(r.created_at)}</time>
                        {r.source_url && <a href={r.source_url} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex items-center gap-1 text-primary hover:underline" data-testid={`link-report-source-${r.id}`}>Source <ExternalLink className="h-3 w-3" aria-hidden /></a>}
                      </div>
                      {canEdit && (
                        <div className="flex gap-1.5">
                          {r.status === "new" && act("reviewed", "Mark reviewed")}
                          {r.status !== "resolved" && act("resolved", "Mark resolved")}
                          {r.status !== "new" && act("new", "Reopen")}
                        </div>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
            {total > LIMIT && (
              <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
                <span>{offset + 1}-{Math.min(offset + LIMIT, total)} of {total}</span>
                <div className="flex gap-1.5">
                  <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))} className="rounded-md border px-2.5 py-1 disabled:opacity-40" data-testid="button-reports-prev">Previous</button>
                  <button type="button" disabled={offset + LIMIT >= total} onClick={() => setOffset(offset + LIMIT)} className="rounded-md border px-2.5 py-1 disabled:opacity-40" data-testid="button-reports-next">Next</button>
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
