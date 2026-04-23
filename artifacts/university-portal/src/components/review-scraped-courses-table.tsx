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

function EvidencePanel({ evidence }: { evidence: ReviewEvidenceItem[] }) {
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

  if (grouped.length === 0) {
    return <div className="text-xs text-gray-400 italic px-3 py-2">No evidence recorded for this course.</div>;
  }

  return (
    <div className="bg-slate-50 border-t border-slate-200">
      <div className="p-3 space-y-3">
        <div className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
          Evidence sources ({evidence.length} total across {grouped.length} field{grouped.length === 1 ? "" : "s"})
        </div>
        {grouped.map(([fieldKey, items]) => (
          <div key={fieldKey} className="bg-white border border-slate-200 rounded overflow-hidden">
            <div className="px-3 py-1.5 bg-slate-100 border-b border-slate-200 text-xs font-mono font-semibold text-slate-700">
              {fieldKey}
              <span className="ml-2 text-slate-400 font-normal">— {items.length} candidate{items.length === 1 ? "" : "s"}</span>
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
        ))}
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
                      {course.duration ? `${course.duration} ${course.durationTerm || ""}` : <span className="text-gray-300">-</span>}
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
                        <EvidencePanel evidence={course.evidence ?? []} />
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
