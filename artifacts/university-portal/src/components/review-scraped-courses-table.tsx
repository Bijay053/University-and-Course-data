import { Fragment, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertTriangle, ExternalLink, ChevronRight, ChevronDown } from "lucide-react";

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
};

interface Props {
  courses: ReviewStagedCourse[];
  /** When true, hides Approve / Reject / Edit / selection controls. */
  readOnly?: boolean;
  /** When true, exposes a "Sources" toggle on each row that reveals
   *  evidence grouped by field_key. Requires `course.evidence` to be
   *  populated by the API. */
  showEvidence?: boolean;
}

function feeDisplay(c: ReviewStagedCourse) {
  if (c.internationalFee == null || c.internationalFee === "") return null;
  const sym = c.currency === "GBP" ? "\u00A3" : c.currency === "USD" ? "$" : "A$";
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
 * Evidence field_keys arrive as snake_case from the API (e.g. "ielts_overall",
 * "international_fee"). The switch normalises them to camelCase first so they
 * match the TypeScript course object properties.
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

/**
 * Loose equality: trim, lower-case, strip currency markers, compare as
 * numeric when both sides parse, otherwise string compare. Used to decide
 * whether the `selected=true` evidence row actually matches the value the
 * scraper persisted on the course.
 */
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
  // year vs years tolerance
  return na.replace(/s$/, "") === nb.replace(/s$/, "");
}

function EvidencePanel({ evidence, course }: { evidence: ReviewEvidenceItem[]; course?: ReviewStagedCourse }) {
  // tracks which individually-suppressed fields the user has opted to show
  const [enabledSuppressed, setEnabledSuppressed] = useState<Set<string>>(new Set());

  const grouped = useMemo(() => {
    const m = new Map<string, ReviewEvidenceItem[]>();
    for (const e of evidence) {
      const arr = m.get(e.fieldKey) ?? [];
      arr.push(e);
      m.set(e.fieldKey, arr);
    }
    // already pre-sorted by API: field_key ASC, selected DESC, decision_score DESC
    return Array.from(m.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [evidence]);

  // A field group is "suppressed" when nothing was selected AND the final value
  // on the course record is empty — i.e. the extractor found a candidate value
  // but negative-suppression (or coherence gates) rejected it entirely.
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
                          “{e.snippet}”
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

export function ReviewScrapedCoursesTable({ courses, readOnly, showEvidence }: Props) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const toggle = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  return (
    <div className="border rounded-lg overflow-hidden">
      {readOnly ? (
        <div className="px-3 py-1.5 bg-amber-50 border-b border-amber-200 text-xs text-amber-800">
          Read-only (historical record) — actions are disabled. Click <span className="font-semibold">Sources</span> to inspect evidence per field.
        </div>
      ) : null}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b">
            <tr>
              {showEvidence ? <th className="p-2 w-8" /> : null}
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
            </tr>
          </thead>
          <tbody className="divide-y">
            {courses.map((course) => {
              const isOpen = expanded.has(course.id);
              const evidenceCount = course.evidence?.length ?? 0;
              return (
                <Fragment key={course.id}>
                  <tr className="hover:bg-gray-50">
                    {showEvidence ? (
                      <td className="p-1 align-top">
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-6 w-6 text-slate-500 hover:bg-slate-100"
                          onClick={() => toggle(course.id)}
                          title={`${isOpen ? "Hide" : "Show"} ${evidenceCount} evidence row${evidenceCount === 1 ? "" : "s"}`}
                          disabled={evidenceCount === 0}
                        >
                          {isOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                        </Button>
                      </td>
                    ) : null}
                    <td className="p-2 align-top">
                      <div className="flex items-start gap-1 min-w-[280px] max-w-[420px]">
                        <span className="font-medium text-gray-800 break-words" title={course.courseName ?? undefined}>
                          {course.courseName ?? "—"}
                        </span>
                        {course.courseWebsite && (
                          <a
                            href={course.courseWebsite}
                            target="_blank"
                            rel="noopener noreferrer"
                            title={`Verify: ${course.courseWebsite}`}
                            className="flex-shrink-0 text-blue-400 hover:text-blue-600 transition-colors mt-1"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <ExternalLink className="w-3.5 h-3.5" />
                          </a>
                        )}
                      </div>
                      {course.category && (
                        <div className="text-xs text-gray-400 break-words">{course.category}</div>
                      )}
                      <div className="flex flex-wrap gap-1 mt-1">
                        {course.autoPublishStatus && (
                          <Badge variant="outline" title="Auto-publish decision" className={`text-[10px] ${
                            course.autoPublishStatus === "approved" ? "text-green-700 border-green-200" :
                            course.autoPublishStatus === "rejected" ? "text-red-700 border-red-200" :
                            "text-amber-700 border-amber-200"
                          }`}>
                            Publish: {course.autoPublishStatus === "approved" ? "ready" : course.autoPublishStatus === "pending_review" ? "review" : course.autoPublishStatus}
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
                        course.intakeMonths.map(m => m.slice(0, 3)).join(", ")
                      ) : <MissingBadge title="Missing intake months" />}
                    </td>
                    <td className="p-2 text-xs text-gray-600 align-top">
                      {course.courseLocation || <span className="text-gray-300">-</span>}
                    </td>
                    <td className="p-2 text-xs text-gray-600 align-top">
                      {course.studyMode || <span className="text-gray-300">-</span>}
                    </td>
                  </tr>
                  {showEvidence && isOpen ? (
                    <tr>
                      <td colSpan={15} className="p-0">
                        <EvidencePanel evidence={course.evidence ?? []} course={course} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
            {courses.length === 0 ? (
              <tr><td colSpan={showEvidence ? 15 : 14} className="p-4 text-center text-gray-400">No courses recorded.</td></tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}
