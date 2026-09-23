import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronLeft, ChevronRight, ExternalLink, Loader2, RefreshCw, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useCan } from "@/components/can";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";

type CourseRef = {
  id: number;
  scrapeJobId: string;
  universityId: number;
  courseName: string | null;
  courseWebsite: string | null;
  scrapeWarnings?: string[] | null;
};

type SourceEvidence = {
  url: string;
  finalUrl?: string;
  verified: boolean;
  status: number | null;
  title: string | null;
  awards: string[];
  reason: string | null;
};

type Review = {
  revision: number;
  history?: ReviewHistoryEntry[] | null;
  historyCount?: number;
  evidenceStale?: boolean;
  evidence?: {
    checkedAt: string;
    original: SourceEvidence;
    candidate: SourceEvidence | null;
    suggestion: Decision | null;
    reason: string;
    referenceOnly: boolean;
  };
  decision?: Decision | null;
  reviewer?: { name?: string; email?: string } | null;
  decidedAt?: string | null;
};

type ReviewHistoryEntry = {
  type: string;
  at?: string;
  evidence?: Review["evidence"];
  decision?: Decision;
  reviewer?: Review["reviewer"];
  evidenceRevision?: number;
  fromUrl?: string | null;
  toUrl?: string | null;
};

type ReviewRow = {
  id: number;
  jobId: string;
  universityId: number;
  courseName: string;
  courseUrl: string | null;
  warningPresent: boolean;
  review: Review;
};

type Decision = "keep_separate" | "not_counterpart" | "current_counterpart";

const DECISIONS: { value: Decision; label: string }[] = [
  { value: "keep_separate", label: "Keep separate" },
  { value: "not_counterpart", label: "Not a counterpart" },
  { value: "current_counterpart", label: "Current counterpart" },
];

function historyTime(value?: string) {
  if (!value || Number.isNaN(Date.parse(value))) return "Timestamp unavailable";
  return new Date(value).toLocaleString();
}

function ReviewHistory({ review, rowId, jobId, universityId }: { review: Review; rowId: number; jobId: string; universityId: number }) {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<ReviewHistoryEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useState({ value: 0 })[0];
  const inlineHistory = review.history ?? [];
  const historyCount = review.historyCount ?? inlineHistory.length;
  const [legacyPage, setLegacyPage] = useState(0);
  const loadHistory = useCallback(async (cursor: number, replace = false) => {
    const id = ++requestId.value;
    setLoading(true); setError(null);
    try {
      const params = new URLSearchParams({ universityId: String(universityId), jobId, revision: String(review.revision), cursor: String(cursor), limit: "50" });
      const response = await fetch(`/api/scrape/staged/dated-catalogue-reviews/${rowId}/history?${params}`);
      if (!response.ok) throw new Error(await getFetchErrorMessage(response));
      const payload = await readResponseJson<{ entries?: ReviewHistoryEntry[]; nextCursor?: number | null }>(response);
      if (id !== requestId.value) return;
      setEntries((current) => replace ? (payload?.entries ?? []) : [...current, ...(payload?.entries ?? [])]);
      setNextCursor(payload?.nextCursor ?? null);
    } catch (reason) {
      if (id === requestId.value) setError(reason instanceof Error ? reason.message : "Could not load review history");
    } finally {
      if (id === requestId.value) setLoading(false);
    }
  }, [jobId, review.revision, rowId, universityId, requestId]);
  useEffect(() => {
    setOpen(false); setEntries([]); setNextCursor(null); setError(null); requestId.value++; setLegacyPage(0);
  }, [review.revision, rowId, jobId, universityId, requestId]);
  const toggle = () => {
    const value = !open; setOpen(value);
    if (value && historyCount > 0 && entries.length === 0) {
      if (review.history) {
        setLegacyPage(0);
        setEntries(review.history.slice().reverse().slice(0, 10));
        setNextCursor(review.history.length > 10 ? 10 : null);
      } else void loadHistory(0, true);
    }
  };
  return (
    <div className="mt-3 border-t border-slate-200 pt-3 text-xs">
      <p className="font-medium text-slate-800" data-testid={`text-current-revision-${rowId}`}>
        Current revision {review.revision} · {review.evidenceStale
          ? "Evidence stale — re-audit required"
          : review.decision
            ? DECISIONS.find((item) => item.value === review.decision)?.label
            : "No current reviewer decision"}
      </p>
      <Button
        type="button" variant="ghost" size="sm" className="mt-1"
        aria-expanded={open} aria-controls={`dated-history-${rowId}`}
        onClick={toggle}
      >
        {open ? "Hide" : "Show"} review history ({historyCount})
      </Button>
      <div id={`dated-history-${rowId}`} hidden={!open}>
        {open && <>
          <p className="mb-2 text-slate-600">
            Read-only history, newest first. A new audit or source URL change clears the previous confirmation.
            Superseded decisions do not apply to the current revision. Courses and publication warnings are unchanged by this timeline.
          </p>
          {loading && <p className="flex items-center gap-2"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading history…</p>}
          {error && <div className="flex items-center gap-2 text-red-700"><span>{error}</span><Button type="button" size="sm" variant="ghost" onClick={() => void loadHistory(0, true)}>Retry</Button></div>}
          {!loading && !error && historyCount === 0 ? <p>No review history recorded.</p> : !loading && !error && (
            <ol className="space-y-3 border-l border-slate-200 pl-3" aria-label="Review history">
              {entries.map((entry, index) => {
                const currentDecision = entry.type === "reviewer_decision"
                  && !review.evidenceStale && !!review.decision
                  && entry.decision === review.decision && entry.at === review.decidedAt
                  && entry.evidenceRevision === review.revision - 1;
                const currentAudit = entry.type === "official_source_audit"
                  && !review.evidenceStale && !!entry.evidence
                  && entry.evidence.checkedAt === review.evidence?.checkedAt
                  ;
                return (
                  <li key={`${entry.at ?? "unknown"}-${index}`} className="space-y-1 break-words">
                    <p className="font-medium">
                      {entry.type === "official_source_audit" ? "Official-source audit"
                        : entry.type === "reviewer_decision" ? "Reviewer decision"
                          : entry.type === "source_url_changed" ? "Source URL changed" : "Historical event"}
                      {" · "}{currentDecision ? "Current decision" : currentAudit ? "Current evidence"
                        : entry.type === "reviewer_decision" ? "Superseded decision"
                          : entry.type === "official_source_audit" ? "Superseded evidence" : "Historical record"}
                    </p>
                    <p className="text-slate-500">{historyTime(entry.at)}</p>
                    {entry.type === "official_source_audit" && <>
                      <p>{entry.evidence?.reason || "No audit reason recorded."}</p>
                      {([{ label: "Archived/original source", evidence: entry.evidence?.original }, { label: "Yearless candidate", evidence: entry.evidence?.candidate }]).map(({ label, evidence }) => {
                        return <div key={label} className="text-slate-600">
                          <p>{label}: {evidence ? (evidence.verified ? "Verified" : "Unverified") : "Not recorded"}
                            {evidence?.title ? ` · ${evidence.title}` : ""}</p>
                          {evidence && <p className="break-all">{evidence.url}</p>}
                          {evidence?.reason && <p>{evidence.reason}</p>}
                        </div>;
                      })}
                    </>}
                    {entry.type === "reviewer_decision" && <>
                      <p>{DECISIONS.find((item) => item.value === entry.decision)?.label || "Decision not recorded"}
                        {" · "}By {entry.reviewer?.name || entry.reviewer?.email || "reviewer"}</p>
                      {entry.evidenceRevision != null && <p>Based on revision {entry.evidenceRevision}</p>}
                    </>}
                    {entry.type === "source_url_changed" && <>
                      <p className="break-all">From: {entry.fromUrl || "No URL"}</p>
                      <p className="break-all">To: {entry.toUrl || "No URL"}</p>
                    </>}
                  </li>
                );
              })}
            </ol>
          )}
          {(nextCursor != null || (review.history && historyCount > 10)) && <div className="mt-3 flex items-center gap-2">
            {review.history && <Button type="button" size="sm" variant="outline" disabled={legacyPage === 0} onClick={() => {
              const page = legacyPage - 1;
              setLegacyPage(page);
              setEntries(inlineHistory.slice().reverse().slice(page * 10, page * 10 + 10));
              setNextCursor((page + 1) * 10 < historyCount ? (page + 1) * 10 : null);
            }}>Newer history</Button>}
            {review.history ? <Button type="button" size="sm" variant="outline" disabled={(legacyPage + 1) * 10 >= historyCount} onClick={() => {
              const page = legacyPage + 1;
              setLegacyPage(page);
              setEntries(inlineHistory.slice().reverse().slice(page * 10, page * 10 + 10));
              setNextCursor((page + 1) * 10 < historyCount ? (page + 1) * 10 : null);
            }}>Older history</Button> : <Button type="button" size="sm" variant="outline" onClick={() => void loadHistory(nextCursor ?? 0)}>Load more history</Button>}
            <span aria-live="polite">{review.history ? `History page ${legacyPage + 1} of ${Math.ceil(historyCount / 10)}` : `${entries.length} of ${historyCount} loaded`}</span>
          </div>}
        </>}
      </div>
    </div>
  );
}

export function DatedCatalogueReview({ courses, readOnly = false }: { courses: CourseRef[]; readOnly?: boolean }) {
  const { can } = useCan();
  const editable = can("staged.edit") && !readOnly;
  const allDated = useMemo(
    () => courses.filter((course) => (
      course.scrapeWarnings?.includes("dated_catalogue_page_review")
      || (
        /^https:\/\/(?:www\.)?winchester\.ac\.uk\//i.test(course.courseWebsite ?? "")
        && /(?:\/|-)20\d{2}(?:\/|-|$)/.test(course.courseWebsite ?? "")
      )
    )),
    [courses],
  );
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(allDated.length / 50));
  const dated = useMemo(() => allDated.slice(page * 50, page * 50 + 50), [allDated, page]);
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [auditing, setAuditing] = useState(false);
  const [savingId, setSavingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const requestBody = useMemo(() => {
    if (dated.length === 0) return null;
    return {
      universityId: dated[0].universityId,
      rows: dated.map((course) => ({ id: course.id, jobId: course.scrapeJobId })),
    };
  }, [dated]);

  useEffect(() => {
    if (page >= pageCount) setPage(pageCount - 1);
  }, [page, pageCount]);

  const load = useCallback(async (preserveError = false) => {
    if (!requestBody) {
      setRows([]);
      return;
    }
    setLoading(true);
    if (!preserveError) setError(null);
    try {
      const response = await fetch("/api/scrape/staged/dated-catalogue-reviews/read", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      if (!response.ok) throw new Error(await getFetchErrorMessage(response));
      const payload = await readResponseJson<{ rows: ReviewRow[] }>(response);
      setRows(payload?.rows ?? []);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load dated catalogue reviews");
    } finally {
      setLoading(false);
    }
  }, [requestBody]);

  useEffect(() => {
    void load();
  }, [load]);

  const audit = async () => {
    if (!requestBody) return;
    setAuditing(true);
    setError(null);
    try {
      const response = await fetch("/api/scrape/staged/dated-catalogue-reviews/audit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      if (!response.ok) throw new Error(await getFetchErrorMessage(response));
      const payload = await readResponseJson<{ rows: ReviewRow[] }>(response);
      setRows(payload?.rows ?? []);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Official-source audit failed");
    } finally {
      setAuditing(false);
    }
  };

  const decide = async (row: ReviewRow, decision: Decision) => {
    setSavingId(row.id);
    setError(null);
    try {
      const response = await fetch(`/api/scrape/staged/dated-catalogue-reviews/${row.id}/decision`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          universityId: row.universityId,
          jobId: row.jobId,
          expectedRevision: row.review.revision,
          decision,
        }),
      });
      if (!response.ok) throw new Error(await getFetchErrorMessage(response));
      const updated = await readResponseJson<ReviewRow>(response);
      if (updated) setRows((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save reviewer decision");
      await load(true);
    } finally {
      setSavingId(null);
    }
  };

  if (allDated.length === 0) return null;

  return (
    <section className="mb-4 rounded-lg border border-amber-300 bg-amber-50 p-4" data-testid="panel-dated-catalogue-review">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 font-semibold text-amber-950">
            <AlertTriangle className="h-4 w-4" />
            Dated Winchester catalogue review
            <Badge variant="outline">{allDated.length}</Badge>
          </h3>
          <p className="mt-1 max-w-3xl text-xs text-amber-900">
            Compare each archived URL with a bounded, same-host official candidate. Suggestions are reference only:
            no course is merged, hidden, redirected, renamed, or published, and the dated warning remains in place.
          </p>
          {pageCount > 1 && <p className="mt-1 text-xs font-medium text-amber-950">Showing bounded rows {page * 50 + 1}–{Math.min((page + 1) * 50, allDated.length)} of {allDated.length}.</p>}
        </div>
        {editable && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void audit()}
            disabled={auditing || loading || savingId !== null}
            data-testid="button-audit-dated-catalogue"
          >
            {auditing ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="mr-1 h-3.5 w-3.5" />}
            {auditing ? "Checking official pages…" : "Audit official pages"}
          </Button>
        )}
      </div>

      {loading && (
        <p className="mt-3 flex items-center gap-2 text-sm text-amber-900" data-testid="status-dated-review-loading">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading saved decisions…
        </p>
      )}
      {error && (
        <div className="mt-3 flex items-center justify-between gap-2 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-800" data-testid="status-dated-review-error">
          <span>{error}</span>
          <Button type="button" size="sm" variant="ghost" onClick={() => void load()} data-testid="button-retry-dated-review">
            <RefreshCw className="mr-1 h-3.5 w-3.5" /> Retry
          </Button>
        </div>
      )}
      {pageCount > 1 && (
        <div className="mt-3 flex items-center justify-end gap-2">
          <Button type="button" size="sm" variant="outline" disabled={page === 0 || loading || auditing || savingId !== null} onClick={() => setPage((value) => value - 1)} data-testid="button-dated-page-previous">
            <ChevronLeft className="h-3.5 w-3.5" /> Previous
          </Button>
          <span className="text-xs text-amber-950" data-testid="text-dated-page">Page {page + 1} of {pageCount}</span>
          <Button type="button" size="sm" variant="outline" disabled={page + 1 >= pageCount || loading || auditing || savingId !== null} onClick={() => setPage((value) => value + 1)} data-testid="button-dated-page-next">
            Next <ChevronRight className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}

      {!loading && rows.length > 0 && (
        <div className="mt-3 space-y-3">
          {rows.map((row) => {
            const evidence = row.review.evidence;
            return (
              <article key={row.id} className="rounded-md border border-amber-200 bg-white p-3" data-testid={`card-dated-review-${row.id}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <strong className="text-sm" data-testid={`text-dated-course-${row.id}`}>{row.courseName}</strong>
                  <Badge variant="outline">Row {row.id} · job {row.jobId}</Badge>
                </div>
                {row.courseUrl && (
                  <a
                    href={row.courseUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-2 flex break-all text-xs text-blue-700 hover:underline"
                    data-testid={`link-staged-original-${row.id}`}
                  >
                    Staged/original URL: {row.courseUrl}<ExternalLink className="ml-1 h-3 w-3 shrink-0" />
                  </a>
                )}
                {!evidence ? (
                  <p className="mt-2 text-xs text-slate-600">Not checked yet. No counterpart is asserted.</p>
                ) : (
                  <>
                    <p className="mt-2 text-xs text-slate-600" data-testid={`text-checked-at-${row.id}`}>
                      Checked {new Date(evidence.checkedAt).toLocaleString()} · {evidence.reason}
                    </p>
                    {row.review.evidenceStale && (
                      <p className="mt-2 rounded border border-red-200 bg-red-50 p-2 text-xs font-medium text-red-800" data-testid={`status-stale-evidence-${row.id}`}>
                        Course URL changed after this audit. This evidence and its prior decision are stale; audit the current URL again.
                      </p>
                    )}
                    <div className="mt-2 grid gap-2 lg:grid-cols-2">
                      {[{ label: "Archived/original source", source: evidence.original }, { label: "Yearless candidate", source: evidence.candidate }].map(({ label, source }) => (
                        <div key={label} className="rounded border border-slate-200 p-2 text-xs">
                          <div className="font-medium">{label}</div>
                          {source ? (
                            <>
                              <a href={source.url} target="_blank" rel="noopener noreferrer" className="mt-1 flex break-all text-blue-700 hover:underline" data-testid={`link-${label === "Yearless candidate" ? "candidate" : "original"}-${row.id}`}>
                                {source.url}<ExternalLink className="ml-1 h-3 w-3 shrink-0" />
                              </a>
                              {source.finalUrl && source.finalUrl !== source.url && (
                                <a
                                  href={source.finalUrl}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="mt-1 flex break-all text-slate-600 hover:underline"
                                  data-testid={`link-${label === "Yearless candidate" ? "candidate" : "original"}-final-${row.id}`}
                                >
                                  Redirect destination: {source.finalUrl}<ExternalLink className="ml-1 h-3 w-3 shrink-0" />
                                </a>
                              )}
                              <div className="mt-1" data-testid={`text-source-title-${row.id}-${label}`}>
                                {source.verified ? source.title : `Unverified: ${source.reason}`}
                              </div>
                              <div className="mt-1 text-slate-500">Awards: {source.awards.length > 0 ? source.awards.join(", ") : "none verified"}</div>
                            </>
                          ) : <div className="mt-1 text-slate-500">No candidate derived</div>}
                        </div>
                      ))}
                    </div>
                    {evidence.suggestion && !row.review.evidenceStale && (
                      <p className="mt-2 text-xs font-medium text-indigo-800">
                        Conservative suggestion: {DECISIONS.find((item) => item.value === evidence.suggestion)?.label}. Reviewer confirmation is still required.
                      </p>
                    )}
                  </>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {DECISIONS.map((option) => (
                    <Button
                      key={option.value}
                      type="button"
                      size="sm"
                      variant={row.review.decision === option.value ? "default" : "outline"}
                      disabled={
                        !editable
                        || !evidence
                        || row.review.evidenceStale
                        || loading
                        || auditing
                        || savingId !== null
                        || (option.value === "current_counterpart" && evidence.suggestion !== "current_counterpart")
                      }
                      onClick={() => void decide(row, option.value)}
                      data-testid={`button-decision-${option.value}-${row.id}`}
                    >
                      {savingId === row.id && row.review.decision !== option.value && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
                      {option.label}
                    </Button>
                  ))}
                  {!editable && (
                    <span className="text-xs text-slate-500">
                      {readOnly ? "Read-only historical view." : "Read-only: staged.edit permission required."}
                    </span>
                  )}
                  {row.review.decision && (
                    <span className="text-xs text-slate-600" data-testid={`text-saved-decision-${row.id}`}>
                      Saved by {row.review.reviewer?.name || row.review.reviewer?.email || "reviewer"}
                      {row.review.decidedAt ? ` on ${new Date(row.review.decidedAt).toLocaleString()}` : ""}
                    </span>
                  )}
                </div>
                <ReviewHistory review={row.review} rowId={row.id} jobId={row.jobId} universityId={row.universityId} />
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}