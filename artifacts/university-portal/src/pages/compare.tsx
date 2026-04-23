import { useEffect, useMemo, useState } from "react";
import { Link, useSearch } from "wouter";
import { ArrowLeft, ExternalLink, Loader2, Download } from "lucide-react";
import { Button } from "@/components/ui/button";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

type EnglishReq = {
  test_type: string; test_name: string | null; overall: number | null;
  listening: number | null; reading: number | null; writing: number | null; speaking: number | null;
};
type AcademicReq = {
  academic_level: string | null; academic_score: number | null;
  score_type: string | null; academic_country: string | null;
};
type CompareCourse = {
  id: number;
  course_name: string;
  university: { id: number; name: string; logo_url: string | null; city: string | null; country: string | null; website: string | null };
  course_location: string | null;
  degree_level: string | null;
  category: string | null;
  sub_category: string | null;
  duration: number | null;
  duration_term: string | null;
  study_mode: string | null;
  intakes: string[];
  international_fee: number | null;
  currency: string | null;
  fee_term: string | null;
  application_fee: number | null;
  course_url: string | null;
  english_requirements: EnglishReq[];
  academic_requirements: AcademicReq[];
};

function formatFee(c: CompareCourse): string | null {
  if (c.international_fee == null) return null;
  const cur = c.currency || "AUD";
  const t = c.fee_term ? ` / ${c.fee_term}` : "";
  return `${cur} ${Math.round(c.international_fee).toLocaleString()}${t}`;
}
function formatDuration(c: CompareCourse): string | null {
  if (c.duration == null) return null;
  const unit = c.duration_term || "Year";
  return `${c.duration} ${unit}${c.duration !== 1 && !unit.endsWith("s") ? "s" : ""}`;
}

const ENGLISH_TEST_LABELS: Record<string, string> = {
  IELTS: "IELTS", PTE: "PTE", TOEFL: "TOEFL",
  CAE: "Cambridge CAE", Cambridge: "Cambridge CAE", "Cambridge CAE": "Cambridge CAE",
  Duolingo: "Duolingo", DET: "Duolingo",
};
function normalizeTestType(t: string): string {
  if (t === "Cambridge" || t === "Cambridge CAE" || t === "CAE") return "CAE";
  if (t === "Duolingo" || t === "DET") return "DET";
  return t;
}

export default function ComparePage() {
  const search = useSearch();
  const ids = new URLSearchParams(search).get("ids") || "";
  const [courses, setCourses] = useState<CompareCourse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!ids) { setLoading(false); return; }
    let cancelled = false;
    (async () => {
      setLoading(true); setError(null);
      try {
        const res = await fetch(`${BASE}/api/search/compare?ids=${encodeURIComponent(ids)}`);
        if (!res.ok) throw new Error(await res.text());
        const json = await res.json() as { courses: CompareCourse[] };
        if (!cancelled) setCourses(json.courses);
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [ids]);

  // Build the english requirements per course, keyed by normalized test type.
  const englishMap = useMemo(() => courses.map((c) => {
    const m = new Map<string, EnglishReq>();
    for (const e of c.english_requirements) {
      const k = normalizeTestType(e.test_type);
      if (!m.has(k)) m.set(k, e);
    }
    return m;
  }), [courses]);

  // Determine which english tests have at least one course providing data —
  // hide rows where every column is null.
  const englishTestsToShow = useMemo(() => {
    const tests: string[] = [];
    for (const t of ["IELTS", "PTE", "TOEFL", "CAE", "DET"]) {
      const anyHas = englishMap.some((m) => {
        const r = m.get(t);
        return !!(r && (r.overall != null || r.listening != null || r.reading != null || r.writing != null || r.speaking != null));
      });
      if (anyHas) tests.push(t);
    }
    return tests;
  }, [englishMap]);

  // Helper to decide if any course has a value for a given column.
  const anyHas = (vals: Array<unknown>) => vals.some((v) => v != null && v !== "" && !(Array.isArray(v) && v.length === 0));

  if (!ids) {
    return (
      <div className="text-center py-12">
        <p className="text-gray-500">No courses selected to compare.</p>
        <Link href="/search">
          <Button variant="outline" className="mt-4"><ArrowLeft className="w-4 h-4 mr-1" /> Back to search</Button>
        </Link>
      </div>
    );
  }

  if (loading) return (
    <div className="text-center py-20 text-gray-500 flex items-center justify-center gap-2">
      <Loader2 className="w-5 h-5 animate-spin" /> Loading comparison…
    </div>
  );

  if (error) return (
    <div className="border rounded-xl bg-red-50 text-red-700 px-4 py-3 text-sm">
      Failed to load comparison: {error}
    </div>
  );

  if (courses.length === 0) return <p className="text-gray-500">No matching courses found.</p>;

  const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <tr className="border-t">
      <td className="py-3 px-4 font-medium text-sm text-gray-600 bg-gray-50 align-top w-44 sticky left-0">{label}</td>
      {children}
    </tr>
  );

  // Conditional row helper — only renders if any course has data.
  const condRow = (label: string, vals: Array<unknown>, render: (i: number) => React.ReactNode) =>
    anyHas(vals) ? (
      <Row label={label}>
        {courses.map((c, i) => <td key={c.id} className="py-2 px-4 align-top">{render(i) ?? <span className="text-gray-300">—</span>}</td>)}
      </Row>
    ) : null;

  // Build english band sub-rows for each test that has data.
  const englishRows = englishTestsToShow.flatMap((t) => {
    const label = ENGLISH_TEST_LABELS[t] ?? t;
    const rows: React.ReactNode[] = [];
    const hasOverall = englishMap.some((m) => m.get(t)?.overall != null);
    const hasListening = englishMap.some((m) => m.get(t)?.listening != null);
    const hasReading = englishMap.some((m) => m.get(t)?.reading != null);
    const hasWriting = englishMap.some((m) => m.get(t)?.writing != null);
    const hasSpeaking = englishMap.some((m) => m.get(t)?.speaking != null);
    const push = (subLabel: string, key: keyof EnglishReq) => rows.push(
      <Row key={`${t}-${key}`} label={subLabel}>
        {courses.map((c, i) => {
          const v = englishMap[i].get(t)?.[key];
          return <td key={c.id} className="py-2 px-4 align-top">{v != null ? v as React.ReactNode : <span className="text-gray-300">—</span>}</td>;
        })}
      </Row>
    );
    if (hasOverall) push(`${label}: Overall`, "overall");
    if (hasListening) push(`${label}: Listening`, "listening");
    if (hasReading) push(`${label}: Reading`, "reading");
    if (hasWriting) push(`${label}: Writing`, "writing");
    if (hasSpeaking) push(`${label}: Speaking`, "speaking");
    return rows;
  });

  return (
    <div className="space-y-4 print:space-y-2">
      <div className="flex items-center justify-between flex-wrap gap-2 print:hidden">
        <div className="flex items-center gap-3">
          <Link href="/search">
            <Button variant="outline" size="sm"><ArrowLeft className="w-4 h-4 mr-1" /> Back</Button>
          </Link>
          <h1 className="text-xl font-bold">Compare {courses.length} Course{courses.length === 1 ? "" : "s"}</h1>
        </div>
        <Button
          variant="default"
          size="sm"
          onClick={() => window.print()}
          className="bg-indigo-600 hover:bg-indigo-700"
        >
          <Download className="w-4 h-4 mr-1" /> Download PDF
        </Button>
      </div>

      <div className="hidden print:block mb-4">
        <h1 className="text-2xl font-bold">Course Comparison</h1>
        <p className="text-xs text-gray-500">{new Date().toLocaleDateString()}</p>
      </div>

      <div className="bg-white border rounded-xl overflow-x-auto print:overflow-visible print:border-0">
        <table className="w-full text-sm min-w-[800px]">
          <thead>
            <tr>
              <th className="py-3 px-4 text-left bg-gray-50 sticky left-0 w-44"></th>
              {courses.map((c) => (
                <th key={c.id} className="py-3 px-4 text-left align-top min-w-[240px]">
                  <div className="flex items-start gap-2">
                    {c.university.logo_url ? (
                      <img src={c.university.logo_url} alt="" className="w-10 h-10 object-contain rounded border bg-gray-50 flex-shrink-0" />
                    ) : (
                      <div className="w-10 h-10 rounded border bg-gray-100 flex items-center justify-center text-[9px] text-gray-400 flex-shrink-0">
                        {c.university.name.split(" ").slice(0, 2).map((s) => s[0]).join("")}
                      </div>
                    )}
                    <div className="min-w-0">
                      <Link href={`/courses/${c.id}`}>
                        <div className="font-semibold text-sm leading-tight hover:text-indigo-700 cursor-pointer">{c.course_name}</div>
                      </Link>
                      <Link href={`/universities/${c.university.id}`}>
                        <div className="text-xs text-gray-500 mt-0.5 hover:text-indigo-700 cursor-pointer">{c.university.name}</div>
                      </Link>
                    </div>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {condRow("Location",
              courses.map((c) => c.course_location || c.university.city || c.university.country),
              (i) => courses[i].course_location || [courses[i].university.city, courses[i].university.country].filter(Boolean).join(", "))}

            {condRow("Degree Level", courses.map((c) => c.degree_level), (i) => courses[i].degree_level)}

            {condRow("Category",
              courses.map((c) => c.category || c.sub_category),
              (i) => {
                const c = courses[i];
                if (!c.category && !c.sub_category) return null;
                return `${c.category ?? ""}${c.sub_category ? ` / ${c.sub_category}` : ""}`;
              })}

            {condRow("Duration", courses.map((c) => c.duration), (i) => formatDuration(courses[i]))}

            {condRow("Study Mode", courses.map((c) => c.study_mode), (i) => courses[i].study_mode)}

            {condRow("Intakes", courses.map((c) => c.intakes.length), (i) => courses[i].intakes.join(", "))}

            {condRow("International Fee", courses.map((c) => c.international_fee), (i) => formatFee(courses[i]))}

            {condRow("Application Fee",
              courses.map((c) => c.application_fee),
              (i) => courses[i].application_fee != null ? `${courses[i].currency || "AUD"} ${courses[i].application_fee}` : null)}

            {englishRows}

            {condRow("Academic Requirements",
              courses.map((c) => c.academic_requirements.length),
              (i) => {
                const a = courses[i].academic_requirements;
                if (a.length === 0) return null;
                return (
                  <ul className="space-y-1">
                    {a.map((r, j) => (
                      <li key={j} className="text-xs">
                        {r.academic_level ?? "—"}
                        {r.academic_score != null ? `: ${r.academic_score} ${r.score_type ?? ""}` : ""}
                        {r.academic_country ? ` (${r.academic_country})` : ""}
                      </li>
                    ))}
                  </ul>
                );
              })}

            {condRow("Course Page",
              courses.map((c) => c.course_url),
              (i) => courses[i].course_url ? (
                <a href={courses[i].course_url!} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline inline-flex items-center gap-1 print:text-black">
                  Visit <ExternalLink className="w-3 h-3 print:hidden" />
                </a>
              ) : null)}
          </tbody>
        </table>
      </div>

      <style>{`
        @media print {
          @page { size: A4 landscape; margin: 1cm; }
          body { background: white !important; }
          aside, nav, header, .sidebar, [data-sidebar] { display: none !important; }
          .print\\:hidden { display: none !important; }
          table { font-size: 11px; }
          th, td { padding: 6px 8px !important; }
        }
      `}</style>
    </div>
  );
}
