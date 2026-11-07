export type DocumentReviewChange = { field: string; oldValue?: unknown; newValue?: unknown; quote?: string; page?: number };
export type DocumentReviewRow = { id: string | number; name: string; status: string; reason?: string; changes?: DocumentReviewChange[];
  category?: string; applicability?: string; evidence?: { url?: string; quote: string; page?: number; provenance?: string }[] };
export type DocumentReview = { checked: number; proposed: number; unmatched: number; notChecked?: number; rows: DocumentReviewRow[] };

const show = (v: unknown) => (v === null || v === undefined || v === "" ? "(blank)" : typeof v === "object" ? JSON.stringify(v) : String(v));
const STATUS_TONE: Record<string, string> = {
  proposed: "bg-amber-100 text-amber-900",
  unchanged: "bg-teal-100 text-teal-900",
  matched: "bg-teal-100 text-teal-900",
  unmatched: "bg-slate-200 text-slate-700",
};

export function DocumentReviewView({ review, testId }: { review: DocumentReview; testId: string }) {
  const rows = Array.isArray(review.rows) ? review.rows.filter(r => r && (typeof r.id === "string" || Number.isSafeInteger(r.id))) : [];
  const matched = rows.filter(r => r.applicability === "verified").length;
  const unchanged = rows.filter(r => r.status === "unchanged").length;
  const manual = rows.filter(r => ["unmatched", "unverified", "conflict", "excluded", "not_checked"].includes(r.status)).length;
  return (
    <div className="mt-3 rounded-xl border border-slate-200 bg-white p-3" data-testid={testId}>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[12px]">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Document review</p>
        <span className="font-mono tabular-nums text-slate-700" data-testid={`${testId}-counts`}>
          {Number(review.checked) || 0} checked · {Number(review.proposed) || 0} proposed · {Number(review.unmatched) || 0} unmatched
          {!!review.notChecked && <> · {review.notChecked} not checked</>}
        </span>
      </div>
      <p className="mt-1 text-[12px] text-slate-600">
        {matched} matched · {unchanged} unchanged · {manual} skipped / manual review.
        {" "}This is a preview, not an update receipt. Apply the proposed changes after reviewing the evidence.
      </p>
      {rows.length === 0 ? (
        <p className="mt-2 text-[12px] text-slate-600">No course rows were checked against this document.</p>
      ) : (
        <ul className="mt-2 divide-y divide-slate-100">
          {rows.map(r => (
            <li key={r.id} className="py-2" data-testid={`document-row-${r.id}`} data-status={r.status}>
              <div className="flex items-baseline gap-2 text-[12.5px]">
                <span className="min-w-0 flex-1 break-words font-medium text-slate-900">{String(r.name || r.id)}</span>
                <span className={`shrink-0 rounded-full px-2 py-0.5 font-mono text-[10.5px] ${STATUS_TONE[r.status] ?? "bg-slate-100 text-slate-700"}`}>{String(r.status)}</span>
              </div>
              {r.reason && <p className="mt-0.5 text-[12px] text-slate-600">{String(r.reason)}</p>}
              {r.applicability && <p className="mt-1 text-[12px] text-slate-600">
                {r.category?.replace(/_/g, " ")} · Applicability: {r.applicability}
              </p>}
              {r.evidence?.map((e, index) => (
                <blockquote key={index} className="mt-1 border-l-2 border-slate-200 pl-2 text-[12px] text-slate-600">
                  {e.provenance === "existing-record-and-document" && (
                    <p className="font-medium">Existing record and document evidence — not a live course-page check</p>
                  )}
                  <p className="whitespace-pre-wrap break-words">{e.quote}</p>
                  {e.url && /^https:\/\//i.test(e.url) && <a href={e.url} target="_blank" rel="noopener noreferrer" className="break-all underline">
                    {e.url}{e.page ? ` (page ${e.page})` : ""}
                  </a>}
                </blockquote>
              ))}
              {!!r.changes?.length && (
                <ul className="mt-1.5 space-y-1.5">
                  {r.changes.map((c, ci) => (
                    <li key={ci} className="rounded-md bg-[#f6f4ee] px-2 py-1.5 text-[12px]" data-testid={`document-change-${r.id}-${ci}`}>
                      <span className="font-medium text-slate-800">{c.field.replace(/_/g, " ")}</span>
                      <div className="mt-0.5 grid grid-cols-1 gap-1 sm:grid-cols-2">
                        <span className="break-words text-slate-500"><span className="text-[10.5px] uppercase tracking-wide">Before </span><s>{show(c.oldValue)}</s></span>
                        <span className="break-words text-teal-900"><span className="text-[10.5px] uppercase tracking-wide text-slate-500">After </span>{show(c.newValue)}</span>
                      </div>
                      {c.quote && (
                        <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] text-slate-600">
                          "{String(c.quote)}"{typeof c.page === "number" && Number.isSafeInteger(c.page) && c.page > 0 ? ` (page ${c.page})` : ""}
                        </p>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
