import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertTriangle, ExternalLink, ChevronRight, ChevronDown, RefreshCw, RotateCcw, CheckCircle2, XCircle, Loader2, SearchX, FileSearch, Ban, Globe, FileWarning } from "lucide-react";

export type ReviewEvidenceItem = {
  id: number;
  fieldKey: string;
  candidateValue: string | null;
  normalizedValue?: string | null;
  sourceUrl: string | null;
  pageType: string | null;
  extractionMethod: string | null;
  snippet: string | null;
  confidence: number | null;
  decisionScore?: number | null;
  decisionStatus?: string | null;
  validationStatus?: string | null;
  selected: boolean;
};

export type ReviewStagedCourse = {
  id: number;
  courseName: string | null;
  category: string | null;
  courseWebsite: string | null;
  courseLocation: string | null;
  duration: number | string | null;
  durationTerm: string | null;
  studyMode: string | null;
  degreeLevel: string | null;
  internationalFee: number | string | null;
  feeTerm: string | null;
  currency: string | null;
  ieltsOverall: number | string | null;
  pteOverall: number | string | null;
  toeflOverall: number | string | null;
  cambridgeOverall: number | string | null;
  duolingoOverall: number | string | null;
  intakeMonths: string[] | null;
  autoPublishStatus: string | null;
  eligibilityStatus: string | null;
  notes: string | null;
  completeness: number | null;
  scrapeWarnings?: string[] | null;
  evidence?: ReviewEvidenceItem[];
  /** Count of pending agent_recovery_results rows for this course. */
  recoveryCount?: number;
};

// ---------------------------------------------------------------------------
// Recovery types
// ---------------------------------------------------------------------------

type RecoveryResult = {
  id: number;
  scrapedCourseId: number;
  scrapeRunId: string;
  field: string;
  recoveredValue: string | null;
  sourceUrl: string | null;
  sourceType: string | null;
  evidenceText: string | null;
  confidence: number | null;
  mappingReason: string | null;
  status: string;
  createdAt: string | null;
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface Props {
  courses: ReviewStagedCourse[];
  /** University owning the staged courses, shown to keep review context visible. */
  universityName?: string | null;
  /** When true, hides Approve / Reject / Edit / selection controls. */
  readOnly?: boolean;
  /** When true, exposes a "Sources" toggle on each row that reveals
   *  evidence grouped by field_key. Requires `course.evidence` to be
   *  populated by the API. */
  showEvidence?: boolean;
  /** University ID used by the repair-queue re-scrape button. */
  universityId?: number;
  /** Callback fired when the operator triggers a per-course re-scrape.
   *  Receives the scraped_course id(s) to rescrape. */
  onRescrape?: (courseId: number) => void;
  /** Callback fired after an agent recovery result is applied or rejected.
   *  Use this to invalidate/refresh the parent staged-course query. */
  onCourseUpdated?: () => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function feeDisplay(c: ReviewStagedCourse) {
  if (c.internationalFee == null || c.internationalFee === "") return null;
  const _CURRENCY_SYMBOLS: Record<string, string> = {
    GBP: "£", USD: "$", EUR: "€", NZD: "NZ$", CAD: "CA$", SGD: "S$",
    MYR: "RM ", IDR: "Rp ", THB: "฿", VND: "₫", PHP: "₱",
    INR: "₹", JPY: "¥", CNY: "¥", KRW: "₩",
  };
  const sym = _CURRENCY_SYMBOLS[c.currency ?? ""] ?? (c.currency && c.currency !== "AUD" ? `${c.currency} ` : "A$");
  const num = typeof c.internationalFee === "number"
    ? c.internationalFee.toLocaleString()
    : c.internationalFee;
  return (
    <span className="text-green-700">
      {sym}{num}
      <span className="text-xs text-gray-400 ml-1">/{c.feeTerm || "yr"}</span>
    </span>
  );
}

function MissingBadge({ title }: { title: string }) {
  return (
    <span className="inline-flex items-center gap-0.5 text-amber-600 text-xs font-medium" title={title}>
      <AlertTriangle className="w-3 h-3" />
    </span>
  );
}

const _WARNING_LABELS: Record<string, string> = {
  english_section_detected_scores_blank: "English section found but scores blank — likely image-only page requiring AI vision",
  fee_section_detected_fee_blank: "Fee section found but fee is blank — may require manual entry",
  suspicious_duration: "Duration value looks wrong — please verify on course page",
  no_intake_months: "Intake section found but no months extracted — please verify",
};

function ScrapeWarningsBadge({ warnings }: { warnings: string[] }) {
  if (!warnings.length) return null;
  const tooltipLines = warnings
    .map((w) => _WARNING_LABELS[w] ?? w.replace(/_/g, " "))
    .join("\n");
  return (
    <span
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-50 border border-amber-300 text-amber-700 text-[10px] font-medium cursor-help"
      title={tooltipLines}
    >
      <AlertTriangle className="w-3 h-3 flex-shrink-0" />
      {warnings.length === 1 ? "1 scrape warning" : `${warnings.length} scrape warnings`}
    </span>
  );
}

/** Convert snake_case to camelCase so API field_keys match TS property names. */
function toCamel(s: string): string {
  return s.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase());
}

/**
 * Map evidence field_key → the matching value on the saved course record.
 */
function finalValueForField(course: ReviewStagedCourse, fieldKey: string): string | null {
  const v = (x: unknown): string | null => {
    if (x === null || x === undefined || x === "") return null;
    if (Array.isArray(x)) return x.length > 0 ? x.join(", ") : null;
    return String(x);
  };
  switch (toCamel(fieldKey)) {
    case "courseName":        return v(course.courseName);
    case "courseLocation":    return v(course.courseLocation);
    case "duration": {
      if (course.duration == null || course.duration === "") return null;
      const dn = typeof course.duration === "number" ? course.duration : parseFloat(course.duration as string);
      if (isNaN(dn)) return `${course.duration} ${course.durationTerm ?? ""}`.trim();
      const dr = Math.round(dn * 10) / 10;
      const dd = dr % 1 === 0 ? String(Math.round(dr)) : String(dr);
      return `${dd} ${course.durationTerm ?? "Year"}`.trim();
    }
    case "studyMode":         return v(course.studyMode);
    case "degreeLevel":       return v(course.degreeLevel);
    case "internationalFee":  return course.internationalFee != null && course.internationalFee !== "" ? `${course.currency ?? "AUD"} ${course.internationalFee}` : null;
    case "ieltsOverall":      return v(course.ieltsOverall);
    case "pteOverall":        return v(course.pteOverall);
    case "toeflOverall":      return v(course.toeflOverall);
    case "cambridgeOverall":  return v(course.cambridgeOverall);
    case "duolingoOverall":   return v(course.duolingoOverall);
    case "intakeMonths":      return v(course.intakeMonths);
    default:                  return null;
  }
}

function looselyEqual(a: string | null, b: string | null): boolean {
  if (a == null && b == null) return true;
  if (a == null || b == null) return false;
  const norm = (s: string) => s.toLowerCase().replace(/\s+/g, " ").replace(/\b(aud|a\$|usd|gbp|\$|£)\s*/gi, "").trim();
  const na = norm(a);
  const nb = norm(b);
  if (na === nb) return true;
  const pa = parseFloat(na.replace(/,/g, ""));
  const pb = parseFloat(nb.replace(/,/g, ""));
  if (Number.isFinite(pa) && Number.isFinite(pb)) return Math.abs(pa - pb) < 1e-6;
  return na.replace(/s$/, "") === nb.replace(/s$/, "");
}

const _FIELD_LABELS: Record<string, string> = {
  international_fee: "International Fee",
  ielts_overall: "IELTS Overall",
  intake_months: "Intake Months",
  course_location: "Course Location",
  other_requirement: "Entry Requirements",
};

// Trace status → display config
type TraceStatus = "no_source" | "no_value" | "level_mismatch" | "browser_failed" | "pdf_failed";
const _TRACE_STATUSES = new Set<string>(["no_source", "no_value", "level_mismatch", "browser_failed", "pdf_failed"]);

const _TRACE_CONFIG: Record<TraceStatus, {
  label: string;
  description: string;
  icon: React.ElementType;
  colorClass: string;
  borderClass: string;
}> = {
  no_source: {
    label: "No source found",
    description: "BFS domain search found no candidate pages for this field category.",
    icon: SearchX,
    colorClass: "text-slate-500",
    borderClass: "border-slate-200 bg-slate-50",
  },
  no_value: {
    label: "No value extracted",
    description: "Candidate pages were found but the extractor found no value for this field.",
    icon: FileSearch,
    colorClass: "text-slate-500",
    borderClass: "border-slate-200 bg-slate-50",
  },
  level_mismatch: {
    label: "Degree level mismatch",
    description: "A value was extracted but the mapper rejected it — the source page targets a different degree level.",
    icon: Ban,
    colorClass: "text-amber-600",
    borderClass: "border-amber-200 bg-amber-50",
  },
  browser_failed: {
    label: "Fetch failed",
    description: "The page could not be loaded — it may require JavaScript rendering or Cloudflare protection blocked the request.",
    icon: Globe,
    colorClass: "text-red-500",
    borderClass: "border-red-200 bg-red-50",
  },
  pdf_failed: {
    label: "PDF extraction failed",
    description: "A PDF was found at the source URL but text extraction returned no usable data.",
    icon: FileWarning,
    colorClass: "text-red-500",
    borderClass: "border-red-200 bg-red-50",
  },
};

// ---------------------------------------------------------------------------
// EvidencePanel
// ---------------------------------------------------------------------------

function EvidencePanel({ evidence, course }: { evidence: ReviewEvidenceItem[]; course?: ReviewStagedCourse }) {
  const [enabledSuppressed, setEnabledSuppressed] = useState<Set<string>>(new Set());

  const grouped = useMemo(() => {
    const m = new Map<string, ReviewEvidenceItem[]>();
    for (const e of evidence) {
      const arr = m.get(e.fieldKey) ?? [];
      arr.push(e);
      m.set(e.fieldKey, arr);
    }
    return Array.from(m.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [evidence]);

  const { visibleGrouped, suppressedFields } = useMemo(() => {
    const visible: [string, ReviewEvidenceItem[]][] = [];
    const suppressed: [string, ReviewEvidenceItem[]][] = [];
    for (const entry of grouped) {
      const [fieldKey, items] = entry;
      const hasSelected = items.some((it) => it.selected);
      if (hasSelected) { visible.push(entry); continue; }
      const finalValue = course ? finalValueForField(course, fieldKey) : null;
      const isSuppressed = finalValue == null || finalValue === "";
      if (isSuppressed) {
        suppressed.push(entry);
        if (enabledSuppressed.has(fieldKey)) visible.push(entry);
      } else {
        visible.push(entry);
      }
    }
    return { visibleGrouped: visible, suppressedFields: suppressed };
  }, [grouped, enabledSuppressed, course]);

  const toggleSuppressed = (fieldKey: string) => {
    setEnabledSuppressed((prev) => {
      const next = new Set(prev);
      if (next.has(fieldKey)) next.delete(fieldKey); else next.add(fieldKey);
      return next;
    });
  };

  if (grouped.length === 0) {
    return <div className="text-xs text-gray-400 italic px-3 py-2">No evidence recorded for this course.</div>;
  }

  return (
    <div className="bg-slate-50 border-t border-slate-200">
      <div className="p-3 space-y-3">
        <div className="flex items-center justify-between gap-2">
          <div className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
            Evidence sources ({evidence.length} total across {grouped.length} field{grouped.length === 1 ? "" : "s"})
          </div>
        </div>
        {visibleGrouped.map(([fieldKey, items]) => {
          const finalValue = course ? finalValueForField(course, fieldKey) : null;
          const selected = items.find((it) => it.selected) ?? null;
          const selectedValue = selected?.normalizedValue ?? selected?.candidateValue ?? null;
          const mismatch = !!course && !looselyEqual(finalValue, selectedValue);
          return (
          <div key={fieldKey} className={`bg-white border rounded overflow-hidden ${mismatch ? "border-red-300" : "border-slate-200"}`}>
            <div className={`px-3 py-1.5 border-b text-xs font-mono font-semibold flex items-center gap-2 ${mismatch ? "bg-red-50 border-red-200 text-red-800" : "bg-slate-100 border-slate-200 text-slate-700"}`}>
              <span>{fieldKey}</span>
              <span className={mismatch ? "text-red-600 font-normal" : "text-slate-400 font-normal"}>— {items.length} candidate{items.length === 1 ? "" : "s"}</span>
              {course ? (
                <span className="ml-auto text-[11px] font-sans font-normal flex items-center gap-1.5">
                  {mismatch ? <AlertTriangle className="w-3.5 h-3.5 text-red-600" aria-label="Selected evidence does not match the saved course value" /> : null}
                  <span className={mismatch ? "text-red-700" : "text-slate-500"}>
                    Final on record:&nbsp;
                  </span>
                  <span className={`font-mono ${mismatch ? "text-red-800 font-semibold" : "text-slate-700"}`}>
                    {finalValue ?? <span className="italic text-slate-400 font-normal">(empty)</span>}
                  </span>
                </span>
              ) : null}
            </div>
            <table className="w-full text-xs">
              <tbody>
                {items.map((e) => (
                  <tr
                    key={e.id}
                    className={
                      e.selected
                        ? "bg-emerald-50 border-l-4 border-emerald-400"
                        : "bg-white text-slate-500 hover:bg-slate-50"
                    }
                  >
                    <td className="p-2 w-6 text-center align-top">
                      {e.selected ? (
                        <span className="text-emerald-600 font-bold" title="Selected — value used on the course">✓</span>
                      ) : null}
                    </td>
                    <td className="p-2 align-top">
                      <div className={e.selected ? "font-semibold text-slate-800" : ""}>
                        {e.candidateValue ?? <span className="italic text-slate-400">(empty)</span>}
                      </div>
                      {e.normalizedValue && e.normalizedValue !== e.candidateValue ? (
                        <div className="text-[10px] text-slate-400 mt-0.5">→ {e.normalizedValue}</div>
                      ) : null}
                      {e.snippet ? (
                        <div className="text-[10px] text-slate-500 mt-1 italic line-clamp-2" title={e.snippet}>
                          "{e.snippet}"
                        </div>
                      ) : null}
                    </td>
                    <td className="p-2 align-top whitespace-nowrap text-[11px]">
                      {e.sourceUrl ? (
                        <a
                          href={e.sourceUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-blue-600 hover:text-blue-800 hover:underline inline-flex items-center gap-1"
                          title={e.sourceUrl}
                        >
                          {e.pageType ?? "source"}
                          <ExternalLink className="w-2.5 h-2.5" />
                        </a>
                      ) : (
                        <span>{e.pageType ?? "—"}</span>
                      )}
                      {e.extractionMethod ? (
                        <span className="text-slate-400"> · {e.extractionMethod}</span>
                      ) : null}
                    </td>
                    <td className="p-2 align-top whitespace-nowrap text-[11px] text-right text-slate-500">
                      {e.confidence != null ? (
                        <div>{(e.confidence * 100).toFixed(0)}% conf</div>
                      ) : null}
                      {e.decisionScore != null ? (
                        <div className="text-slate-400">score {e.decisionScore.toFixed(2)}</div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          );
        })}

        {suppressedFields.length > 0 && (
          <div className="border border-slate-200 rounded overflow-hidden">
            <div className="px-3 py-1.5 bg-slate-100 border-b border-slate-200 flex items-center gap-2">
              <span className="text-[11px] font-semibold text-slate-500 uppercase tracking-wide">
                Suppressed fields ({suppressedFields.length})
              </span>
              <span className="text-[11px] text-slate-400">— tick to include in evidence view</span>
            </div>
            <div className="divide-y divide-slate-100">
              {suppressedFields.map(([fieldKey, items]) => {
                const enabled = enabledSuppressed.has(fieldKey);
                const topCandidate = items[0];
                return (
                  <label
                    key={fieldKey}
                    className={`flex items-center gap-3 px-3 py-2 cursor-pointer select-none transition-colors ${
                      enabled ? "bg-amber-50 hover:bg-amber-100" : "bg-white hover:bg-slate-50"
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={enabled}
                      onChange={() => toggleSuppressed(fieldKey)}
                      className="w-3.5 h-3.5 accent-amber-500 shrink-0"
                    />
                    <span className="font-mono text-[11px] text-slate-700 shrink-0">{fieldKey}</span>
                    {topCandidate && (
                      <span className="text-[11px] text-slate-400 truncate">
                        {topCandidate.candidateValue ?? <em>empty</em>}
                        {topCandidate.extractionMethod ? ` · ${topCandidate.extractionMethod}` : ""}
                      </span>
                    )}
                    <span className="ml-auto text-[10px] text-slate-400 shrink-0">
                      {items.length} candidate{items.length === 1 ? "" : "s"}
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// RecoveryPanel — Agent Recovery results for a single staged course
// ---------------------------------------------------------------------------

function ConfidenceBar({ value }: { value: number | null }) {
  if (value == null) return <span className="text-slate-400 text-[10px]">—</span>;
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? "bg-green-500" : pct >= 45 ? "bg-yellow-400" : "bg-red-400";
  const textColor = pct >= 70 ? "text-green-700" : pct >= 45 ? "text-yellow-700" : "text-red-600";
  return (
    <div className="flex items-center gap-1.5">
      <div className="w-14 h-1.5 rounded-full bg-slate-200 overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-[10px] font-mono ${textColor}`}>{pct}%</span>
    </div>
  );
}

function RecoveryPanel({ courseId, readOnly, onAction }: { courseId: number; readOnly?: boolean; onAction?: () => void }) {
  const [results, setResults] = useState<RecoveryResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [acting, setActing] = useState<Record<number, "apply" | "reject">>({});
  const [error, setError] = useState<string | null>(null);

  const fetchResults = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`/api/scrape/recovery/${courseId}`, { credentials: "include" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setResults(data.results ?? []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [courseId]);

  // Load on mount
  useEffect(() => { void fetchResults(); }, [fetchResults]);

  const handleTrigger = async () => {
    setTriggering(true);
    setError(null);
    try {
      const r = await fetch("/api/scrape/recovery/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ scraped_course_id: courseId }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await fetchResults();
    } catch (e) {
      setError(String(e));
    } finally {
      setTriggering(false);
    }
  };

  const handleAction = async (resultId: number, action: "apply" | "reject") => {
    setActing((prev) => ({ ...prev, [resultId]: action }));
    try {
      const r = await fetch(`/api/scrape/recovery/${resultId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ action }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail ?? `HTTP ${r.status}`);
      }
      // Refresh recovery panel and notify parent to refresh staged-course list
      await fetchResults();
      onAction?.();
    } catch (e) {
      setError(String(e));
    } finally {
      setActing((prev) => { const n = { ...prev }; delete n[resultId]; return n; });
    }
  };

  if (loading && results === null) {
    return (
      <div className="flex items-center gap-2 px-4 py-3 text-sm text-slate-500">
        <Loader2 className="w-3.5 h-3.5 animate-spin" />
        Loading recovery results…
      </div>
    );
  }

  const pending  = results?.filter((r) => r.status === "pending") ?? [];
  const actioned = results?.filter((r) => r.status === "applied" || r.status === "rejected") ?? [];
  const traces   = results?.filter((r) => _TRACE_STATUSES.has(r.status)) ?? [];

  // Has the recovery engine been run at least once for this course?
  // True when there are any rows (pending, actioned, or trace).
  const hasRun = (results?.length ?? 0) > 0;

  return (
    <div className="bg-amber-50 border-t border-amber-200">
      {/* Header */}
      <div className="flex items-center justify-between gap-3 px-4 py-2 border-b border-amber-200">
        <div className="flex items-center gap-2">
          <RotateCcw className="w-3.5 h-3.5 text-amber-600" />
          <span className="text-xs font-semibold text-amber-800 uppercase tracking-wide">
            Agent Recovery
          </span>
          {pending.length > 0 && (
            <Badge className="text-[10px] bg-amber-600 text-white border-0 py-0">
              {pending.length} pending
            </Badge>
          )}
          <span className="text-[11px] text-amber-600 italic">
            — recovered values need your approval before they are applied
          </span>
        </div>
        <div className="flex items-center gap-2">
          {!readOnly && (
            <Button
              size="sm"
              variant="outline"
              className="h-6 text-[11px] px-2 text-amber-700 border-amber-300 hover:bg-amber-100"
              onClick={handleTrigger}
              disabled={triggering || loading}
              title="Run a fresh recovery search for this course"
            >
              {triggering ? <Loader2 className="w-3 h-3 animate-spin mr-1" /> : <RotateCcw className="w-3 h-3 mr-1" />}
              Run Recovery
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            className="h-6 text-[11px] px-2 text-amber-600"
            onClick={fetchResults}
            disabled={loading}
          >
            {loading ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
          </Button>
        </div>
      </div>

      {error && (
        <div className="px-4 py-2 text-xs text-red-700 bg-red-50 border-b border-red-200">
          ⚠ {error}
        </div>
      )}

      {/* Never been run yet */}
      {!loading && !hasRun && (
        <div className="px-4 py-3 text-xs text-amber-700 italic">
          No recovery results yet. Click <strong>Run Recovery</strong> to search the university domain for missing field values.
        </div>
      )}

      {/* Pending results table */}
      {pending.length > 0 && (
        <div className="p-3">
          <div className="text-[10px] font-semibold text-amber-700 uppercase tracking-wide mb-2">
            Pending — review and apply or reject each result
          </div>
          <div className="space-y-2">
            {pending.map((res) => {
              const isActing = res.id in acting;
              return (
                <div
                  key={res.id}
                  className="bg-white border border-amber-200 rounded-lg overflow-hidden"
                >
                  {/* Row header */}
                  <div className="flex items-center justify-between gap-2 px-3 py-2 bg-amber-50 border-b border-amber-100">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[11px] font-semibold text-amber-800 bg-amber-100 px-1.5 py-0.5 rounded">
                        {_FIELD_LABELS[res.field] ?? res.field}
                      </span>
                      {res.sourceType === "pdf" ? (
                        <Badge className="text-[10px] bg-amber-100 text-amber-800 border border-amber-300 py-0 hover:bg-amber-100">
                          PDF
                        </Badge>
                      ) : res.sourceType === "pdf_broad" ? (
                        <Badge className="text-[10px] bg-orange-100 text-orange-800 border border-orange-300 py-0 hover:bg-orange-100" title="Found via broad-keyword fallback scorer — PDF not directly linked from a fees/requirements page">
                          PDF (broad)
                        </Badge>
                      ) : (
                        <Badge variant="outline" className="text-[10px] text-slate-500 border-slate-200 py-0">
                          {res.sourceType ?? "html"}
                        </Badge>
                      )}
                    </div>
                    {!readOnly && (
                      <div className="flex items-center gap-1.5">
                        <Button
                          size="sm"
                          className="h-6 text-[11px] px-2 bg-green-600 hover:bg-green-700 text-white"
                          disabled={isActing}
                          onClick={() => handleAction(res.id, "apply")}
                          title="Apply this value to the staged course"
                        >
                          {isActing && acting[res.id] === "apply"
                            ? <Loader2 className="w-3 h-3 animate-spin" />
                            : <CheckCircle2 className="w-3 h-3 mr-1" />}
                          Apply
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-6 text-[11px] px-2 text-red-600 border-red-200 hover:bg-red-50"
                          disabled={isActing}
                          onClick={() => handleAction(res.id, "reject")}
                          title="Reject — dismiss this recovery result"
                        >
                          {isActing && acting[res.id] === "reject"
                            ? <Loader2 className="w-3 h-3 animate-spin" />
                            : <XCircle className="w-3 h-3 mr-1" />}
                          Reject
                        </Button>
                      </div>
                    )}
                  </div>

                  {/* Value + evidence body */}
                  <div className="px-3 py-2 grid grid-cols-1 gap-1.5 text-xs">
                    {/* Recovered value */}
                    <div className="flex items-start gap-3 flex-wrap">
                      <div>
                        <div className="text-[10px] text-slate-400 mb-0.5">Recovered value</div>
                        <div className="font-semibold text-slate-800 font-mono">{res.recoveredValue ?? "—"}</div>
                      </div>
                      <div>
                        <div className="text-[10px] text-slate-400 mb-0.5">Confidence</div>
                        <ConfidenceBar value={res.confidence} />
                      </div>
                      {res.mappingReason && (
                        <div className="flex-1 min-w-[180px]">
                          <div className="text-[10px] text-slate-400 mb-0.5">Mapping reason</div>
                          <div className="text-[11px] text-slate-600 italic">{res.mappingReason}</div>
                        </div>
                      )}
                    </div>

                    {/* Source URL */}
                    {res.sourceUrl && (
                      <div>
                        <div className="text-[10px] text-slate-400 mb-0.5">Source</div>
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <a
                            href={res.sourceUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-blue-600 hover:text-blue-800 hover:underline text-[11px] break-all"
                          >
                            <ExternalLink className="w-2.5 h-2.5 flex-shrink-0" />
                            {res.sourceUrl}
                          </a>
                          {(res.sourceType === "pdf" || res.sourceType === "pdf_broad") && (
                            <span
                              className={`inline-flex items-center text-[10px] font-semibold border px-1.5 py-0.5 rounded flex-shrink-0 ${
                                res.sourceType === "pdf_broad"
                                  ? "bg-orange-100 text-orange-800 border-orange-300"
                                  : "bg-amber-100 text-amber-800 border-amber-300"
                              }`}
                              title={res.sourceType === "pdf_broad" ? "Found via broad-keyword fallback scorer" : undefined}
                            >
                              {res.sourceType === "pdf_broad" ? "PDF (broad)" : "PDF"}
                            </span>
                          )}
                        </div>
                      </div>
                    )}

                    {/* Evidence snippet */}
                    {res.evidenceText && (
                      <div>
                        <div className="text-[10px] text-slate-400 mb-0.5">Evidence text</div>
                        <div className="text-[11px] text-slate-600 italic bg-slate-50 border border-slate-200 rounded px-2 py-1 line-clamp-3">
                          "{res.evidenceText}"
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Search Trace — diagnostic rows explaining WHY recovery found nothing */}
      {hasRun && traces.length > 0 && (
        <div className="px-4 py-3 border-t border-amber-100">
          <div className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide mb-2">
            Search trace — {traces.length} field{traces.length === 1 ? "" : "s"} not recovered
          </div>
          <div className="space-y-1.5">
            {traces.map((res) => {
              const cfg = _TRACE_CONFIG[res.status as TraceStatus] ?? {
                label: res.status,
                description: res.mappingReason ?? "",
                icon: SearchX,
                colorClass: "text-slate-500",
                borderClass: "border-slate-200 bg-slate-50",
              };
              const Icon = cfg.icon;
              return (
                <div
                  key={res.id}
                  className={`flex items-start gap-2.5 px-3 py-2 rounded-lg border text-xs ${cfg.borderClass}`}
                >
                  <Icon className={`w-3.5 h-3.5 mt-0.5 flex-shrink-0 ${cfg.colorClass}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-slate-700">
                        {_FIELD_LABELS[res.field] ?? res.field}
                      </span>
                      <span className={`text-[10px] font-medium ${cfg.colorClass}`}>
                        {cfg.label}
                      </span>
                    </div>
                    <div className="text-[11px] text-slate-500 mt-0.5">
                      {res.mappingReason ?? cfg.description}
                    </div>
                    {res.evidenceText && (
                      <div className="text-[10px] text-slate-400 italic mt-0.5 truncate">
                        {res.evidenceText}
                      </div>
                    )}
                    {res.sourceUrl && (
                      <a
                        href={res.sourceUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-[10px] text-blue-500 hover:text-blue-700 hover:underline mt-0.5 break-all"
                      >
                        <ExternalLink className="w-2 h-2 flex-shrink-0" />
                        {res.sourceUrl.length > 60 ? res.sourceUrl.slice(0, 60) + "…" : res.sourceUrl}
                      </a>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Ran but found nothing at all and no traces (shouldn't normally happen) */}
      {hasRun && pending.length === 0 && traces.length === 0 && actioned.length === 0 && (
        <div className="px-4 py-3 text-xs text-slate-500 italic">
          Recovery ran but found no missing fields for this course. Click <strong>Run Recovery</strong> to search again.
        </div>
      )}

      {/* Actioned results (collapsed summary) */}
      {actioned.length > 0 && (
        <div className="px-4 py-2 border-t border-amber-100">
          <div className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide mb-1.5">
            Previously actioned ({actioned.length})
          </div>
          <div className="flex flex-wrap gap-1.5">
            {actioned.map((res) => (
              <span
                key={res.id}
                className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border ${
                  res.status === "applied"
                    ? "bg-green-50 border-green-200 text-green-700"
                    : "bg-slate-50 border-slate-200 text-slate-500 line-through"
                }`}
                title={`${res.field}: ${res.recoveredValue ?? "—"} (${res.status})`}
              >
                {res.status === "applied" ? <CheckCircle2 className="w-2.5 h-2.5" /> : <XCircle className="w-2.5 h-2.5" />}
                {_FIELD_LABELS[res.field] ?? res.field}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main table
// ---------------------------------------------------------------------------

export function ReviewScrapedCoursesTable({ courses, universityName, readOnly, showEvidence, universityId, onRescrape, onCourseUpdated }: Props) {
  const [rescraping, setRescraping] = useState<Set<number>>(new Set());
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [recoveryOpen, setRecoveryOpen] = useState<Set<number>>(new Set());

  const handleRescrape = async (course: ReviewStagedCourse) => {
    if (!universityId) return;
    setRescraping((prev) => new Set(prev).add(course.id));
    try {
      await fetch("/api/scrape/rescrape-courses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ universityId, scrapedCourseIds: [course.id] }),
        credentials: "include",
      });
      onRescrape?.(course.id);
    } catch {
      /* ignore */
    } finally {
      setRescraping((prev) => { const s = new Set(prev); s.delete(course.id); return s; });
    }
  };

  const toggle = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleRecovery = (id: number) => {
    setRecoveryOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  return (
    <div className="border rounded-lg overflow-hidden">
      {universityName ? (
        <div className="flex items-center gap-2 px-3 py-2 bg-slate-50 border-b text-xs">
          <span className="font-semibold text-slate-500">University</span>
          <span className="font-medium text-slate-800 truncate" title={universityName}>{universityName}</span>
        </div>
      ) : null}
      {readOnly ? (
        <div className="px-3 py-1.5 bg-amber-50 border-b border-amber-200 text-xs text-amber-800">
          Read-only (historical record) — actions are disabled. Click <span className="font-semibold">Sources</span> to inspect evidence per field.
        </div>
      ) : null}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b">
            <tr>
              {showEvidence ? <th className="p-2 text-xs font-medium text-slate-500 text-center whitespace-nowrap">Evidence</th> : null}
              <th className="text-left p-2 font-medium text-gray-600 min-w-[200px]">Course Name</th>
              <th className="text-center p-2 font-medium text-gray-600 w-16">Score</th>
              <th className="text-left p-2 font-medium text-gray-600">Level</th>
              <th className="text-left p-2 font-medium text-gray-600">Duration</th>
              <th className="text-right p-2 font-medium text-gray-600">Intl. Fee</th>
              <th className="text-center p-2 font-medium text-purple-600">IELTS</th>
              <th className="text-center p-2 font-medium text-orange-600">PTE</th>
              <th className="text-center p-2 font-medium text-rose-600">TOEFL</th>
              <th className="text-center p-2 font-medium text-teal-600">CAE</th>
              <th className="text-center p-2 font-medium text-emerald-600">DET</th>
              <th className="text-left p-2 font-medium text-gray-600">Intakes</th>
              <th className="text-left p-2 font-medium text-gray-600">Course Location</th>
              <th className="text-left p-2 font-medium text-gray-600">Mode</th>
              {!readOnly ? (
                <th className="text-center p-2 font-medium text-gray-500 w-24">Actions</th>
              ) : null}
            </tr>
          </thead>
          <tbody className="divide-y">
            {courses.map((course) => {
              const isOpen = expanded.has(course.id);
              const isRecoveryOpen = recoveryOpen.has(course.id);
              const evidenceCount = course.evidence?.length ?? 0;
              const recoveryCount = course.recoveryCount ?? 0;
              const colSpan = (showEvidence ? 1 : 0) + 13 + (readOnly ? 0 : 1);
              return (
                <Fragment key={course.id}>
                  <tr className="hover:bg-gray-50">
                    {showEvidence ? (
                      <td className="p-1 align-top text-center">
                        <Button
                          size="sm"
                          variant="ghost"
                          className={`h-7 px-2 text-xs font-medium gap-1 ${evidenceCount > 0 ? "text-blue-600 hover:bg-blue-50 hover:text-blue-700" : "text-slate-300 cursor-not-allowed"}`}
                          onClick={() => toggle(course.id)}
                          title={`${isOpen ? "Hide" : "Show"} ${evidenceCount} evidence row${evidenceCount === 1 ? "" : "s"}`}
                          disabled={evidenceCount === 0}
                        >
                          {isOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                          {evidenceCount > 0 ? `${evidenceCount}` : "—"}
                        </Button>
                      </td>
                    ) : null}
                    <td className="p-2 align-top">
                      <div className="min-w-[280px] max-w-[420px]">
                        <span className="font-medium text-gray-800 break-words" title={course.courseName ?? undefined}>
                          {course.courseName ?? "—"}
                        </span>
                      </div>
                      {course.courseWebsite && (
                        <a
                          href={course.courseWebsite}
                          target="_blank"
                          rel="noopener noreferrer"
                          title={course.courseWebsite}
                          className="inline-flex items-center gap-1 text-[11px] text-blue-500 hover:text-blue-700 hover:underline mt-0.5 max-w-[420px] truncate"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <ExternalLink className="w-2.5 h-2.5 flex-shrink-0" />
                          <span className="truncate">{course.courseWebsite.replace(/^https?:\/\//, "")}</span>
                        </a>
                      )}
                      {course.category && (
                        <div className="text-xs text-gray-400 break-words">{course.category}</div>
                      )}
                      <div className="flex flex-wrap gap-1 mt-1">
                        {course.autoPublishStatus && (
                          <Badge variant="outline" title="Auto-publish decision" className={`text-[10px] ${
                            course.autoPublishStatus === "approved" ? "text-green-700 border-green-200" :
                            course.autoPublishStatus === "rejected" ? "text-red-700 border-red-200" :
                            course.autoPublishStatus === "data_quality_failure" ? "text-red-800 border-red-400 bg-red-50 font-semibold" :
                            "text-amber-700 border-amber-200"
                          }`}>
                            {course.autoPublishStatus === "data_quality_failure"
                              ? "⛔ Data Quality Failure"
                              : `Publish: ${course.autoPublishStatus === "approved" ? "ready" : course.autoPublishStatus === "pending_review" ? "review" : course.autoPublishStatus}`}
                          </Badge>
                        )}
                        {course.eligibilityStatus && (
                          <Badge variant="outline" title="Eligibility for international on-campus students" className={`text-[10px] ${
                            course.eligibilityStatus === "eligible" ? "text-green-700 border-green-200" :
                            course.eligibilityStatus === "rejected" ? "text-red-700 border-red-200" :
                            "text-amber-700 border-amber-200"
                          }`}>
                            Eligibility: {course.eligibilityStatus}
                          </Badge>
                        )}
                        {/* Agent Recovery toggle — always visible so operators can trigger a fresh pass */}
                        {!readOnly && (
                          <button
                            className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[10px] font-medium transition-colors cursor-pointer ${
                              recoveryCount > 0
                                ? "bg-amber-100 border-amber-300 text-amber-700 hover:bg-amber-200"
                                : "bg-slate-50 border-slate-200 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
                            }`}
                            title={
                              recoveryCount > 0
                                ? `${recoveryCount} recovered value${recoveryCount === 1 ? "" : "s"} available — click to review or trigger again`
                                : "Run Agent Recovery — search the university domain for missing field values"
                            }
                            onClick={() => toggleRecovery(course.id)}
                          >
                            <RotateCcw className="w-2.5 h-2.5 flex-shrink-0" />
                            {recoveryCount > 0 ? `↻ ${recoveryCount} recoverable` : "↻ Recover"}
                          </button>
                        )}
                        {course.scrapeWarnings && course.scrapeWarnings.length > 0 && (
                          <ScrapeWarningsBadge warnings={course.scrapeWarnings} />
                        )}
                      </div>
                      {course.notes && (
                        <div className="text-xs text-amber-600 truncate mt-0.5" title={course.notes}>⚠ {course.notes}</div>
                      )}
                    </td>
                    <td className="p-2 text-center align-top">
                      {course.completeness != null ? (
                        <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-semibold ${
                          course.completeness >= 80 ? "bg-green-100 text-green-700" :
                          course.completeness >= 50 ? "bg-yellow-100 text-yellow-700" :
                          "bg-red-100 text-red-700"
                        }`}>{course.completeness}%</span>
                      ) : <span className="text-gray-300">-</span>}
                    </td>
                    <td className="p-2 align-top">
                      {course.degreeLevel ? (
                        <Badge variant="outline" className="text-xs">{course.degreeLevel}</Badge>
                      ) : <span className="text-gray-300">-</span>}
                    </td>
                    <td className="p-2 text-gray-600 whitespace-nowrap align-top">
                      {course.duration != null && course.duration !== "" ? (() => {
                        const n = typeof course.duration === "number" ? course.duration : parseFloat(course.duration as string);
                        if (isNaN(n)) return `${course.duration} ${course.durationTerm || ""}`.trim();
                        const r = Math.round(n * 10) / 10;
                        const display = r % 1 === 0 ? String(Math.round(r)) : String(r);
                        return `${display} ${course.durationTerm || "Year"}`.trim();
                      })() : <span className="text-gray-300">-</span>}
                    </td>
                    <td className="p-2 text-right font-medium whitespace-nowrap align-top">
                      {feeDisplay(course) ?? <MissingBadge title="Missing international fee" />}
                    </td>
                    <td className="p-2 text-center align-top">
                      {course.ieltsOverall != null && course.ieltsOverall !== "" ? (
                        <span className="text-purple-700 font-medium">{course.ieltsOverall}</span>
                      ) : <MissingBadge title="Missing IELTS Overall" />}
                    </td>
                    <td className="p-2 text-center align-top">
                      {course.pteOverall != null && course.pteOverall !== "" ? (
                        <span className="text-orange-600 font-medium">{course.pteOverall}</span>
                      ) : <span className="text-gray-300 text-xs">-</span>}
                    </td>
                    <td className="p-2 text-center align-top">
                      {course.toeflOverall != null && course.toeflOverall !== "" ? (
                        <span className="text-rose-600 font-medium">{course.toeflOverall}</span>
                      ) : <span className="text-gray-300 text-xs">-</span>}
                    </td>
                    <td className="p-2 text-center align-top">
                      {course.cambridgeOverall != null && course.cambridgeOverall !== "" ? (
                        <span className="text-teal-600 font-medium">{course.cambridgeOverall}</span>
                      ) : <span className="text-gray-300 text-xs">-</span>}
                    </td>
                    <td className="p-2 text-center align-top">
                      {course.duolingoOverall != null && course.duolingoOverall !== "" ? (
                        <span className="text-emerald-600 font-medium">{course.duolingoOverall}</span>
                      ) : <span className="text-gray-300 text-xs">-</span>}
                    </td>
                    <td className="p-2 text-xs text-gray-600 align-top">
                      {course.intakeMonths?.length ? (
                        (Array.isArray(course.intakeMonths)
                          ? course.intakeMonths
                          : String(course.intakeMonths).split(",").map(s => s.trim()).filter(Boolean)
                        ).map(m => String(m).slice(0, 3)).join(", ")
                      ) : <MissingBadge title="Missing intake months" />}
                    </td>
                    <td className="p-2 text-xs text-gray-600 align-top">
                      {course.courseLocation || <span className="text-gray-300">-</span>}
                    </td>
                    <td className="p-2 text-xs text-gray-600 align-top">
                      {course.studyMode || <span className="text-gray-300">-</span>}
                    </td>
                    {!readOnly ? (
                      <td className="p-2 text-center align-top whitespace-nowrap">
                        <div className="flex items-center justify-center gap-1">
                          {course.courseWebsite && (
                            <a
                              href={course.courseWebsite}
                              target="_blank"
                              rel="noopener noreferrer"
                              title="Open course page in new tab"
                            >
                              <Button
                                size="sm"
                                variant="ghost"
                                className="h-7 w-7 p-0 text-blue-500 hover:text-blue-700 hover:bg-blue-50"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <ExternalLink className="w-3.5 h-3.5" />
                              </Button>
                            </a>
                          )}
                          {universityId && (
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7 w-7 p-0 text-amber-600 hover:text-amber-800 hover:bg-amber-50"
                              title="Re-scrape this course"
                              disabled={rescraping.has(course.id)}
                              onClick={() => handleRescrape(course)}
                            >
                              <RefreshCw className={`w-3.5 h-3.5 ${rescraping.has(course.id) ? "animate-spin" : ""}`} />
                            </Button>
                          )}
                        </div>
                      </td>
                    ) : null}
                  </tr>

                  {/* Evidence panel (expand row) */}
                  {showEvidence && isOpen ? (
                    <tr>
                      <td colSpan={colSpan} className="p-0">
                        <EvidencePanel evidence={course.evidence ?? []} course={course} />
                      </td>
                    </tr>
                  ) : null}

                  {/* Recovery panel (inline, toggled by amber badge) */}
                  {isRecoveryOpen ? (
                    <tr>
                      <td colSpan={colSpan} className="p-0">
                        <RecoveryPanel courseId={course.id} readOnly={readOnly} onAction={onCourseUpdated} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
            {courses.length === 0 ? (
              <tr><td colSpan={(showEvidence ? 1 : 0) + 13 + (readOnly ? 0 : 1)} className="p-4 text-center text-gray-400">No courses recorded.</td></tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}
