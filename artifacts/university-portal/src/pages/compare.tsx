import { useEffect, useState } from "react";
import { Link, useSearch } from "wouter";
import { ArrowLeft, ExternalLink, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

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
  english_requirements: Array<{
    test_type: string; test_name: string | null; overall: number | null;
    listening: number | null; reading: number | null; writing: number | null; speaking: number | null;
  }>;
  academic_requirements: Array<{
    academic_level: string | null; academic_score: number | null;
    score_type: string | null; academic_country: string | null;
  }>;
};

function formatFee(c: CompareCourse) {
  if (c.international_fee == null) return "—";
  const cur = c.currency || "AUD";
  const t = c.fee_term ? ` / ${c.fee_term}` : "";
  return `${cur} ${Math.round(c.international_fee).toLocaleString()}${t}`;
}
function formatDuration(c: CompareCourse) {
  if (c.duration == null) return "—";
  const unit = c.duration_term || "Year";
  return `${c.duration} ${unit}${c.duration !== 1 && !unit.endsWith("s") ? "s" : ""}`;
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
      setLoading(true);
      setError(null);
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

  if (loading) {
    return (
      <div className="text-center py-20 text-gray-500 flex items-center justify-center gap-2">
        <Loader2 className="w-5 h-5 animate-spin" /> Loading comparison…
      </div>
    );
  }

  if (error) {
    return (
      <div className="border rounded-xl bg-red-50 text-red-700 px-4 py-3 text-sm">
        Failed to load comparison: {error}
      </div>
    );
  }

  if (courses.length === 0) {
    return <p className="text-gray-500">No matching courses found.</p>;
  }

  // Build english scores map per course (test_type -> overall).
  const englishMap = courses.map((c) => {
    const m: Record<string, number | null> = {};
    for (const e of c.english_requirements) {
      const key = e.test_type === "Cambridge" ? "CAE"
        : e.test_type === "Duolingo" ? "DET" : e.test_type;
      m[key] = e.overall;
    }
    return m;
  });

  const englishTests = ["IELTS", "PTE", "TOEFL", "CAE", "DET"];

  const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <tr className="border-t">
      <td className="py-3 px-4 font-medium text-sm text-gray-600 bg-gray-50 align-top w-44 sticky left-0">{label}</td>
      {children}
    </tr>
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3">
          <Link href="/search">
            <Button variant="outline" size="sm"><ArrowLeft className="w-4 h-4 mr-1" /> Back</Button>
          </Link>
          <h1 className="text-xl font-bold">Compare {courses.length} Course{courses.length === 1 ? "" : "s"}</h1>
        </div>
        <Button
          variant="default"
          size="sm"
          onClick={() => alert("PDF export — coming in Phase 2")}
        >
          Download PDF
        </Button>
      </div>

      <div className="bg-white border rounded-xl overflow-x-auto">
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
                      <div className="font-semibold text-sm leading-tight">{c.course_name}</div>
                      <div className="text-xs text-gray-500 mt-0.5">{c.university.name}</div>
                    </div>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <Row label="Location">
              {courses.map((c) => <td key={c.id} className="py-2 px-4 align-top">{c.course_location || `${c.university.city ?? ""}, ${c.university.country ?? ""}`}</td>)}
            </Row>
            <Row label="Degree Level">
              {courses.map((c) => <td key={c.id} className="py-2 px-4 align-top">{c.degree_level || "—"}</td>)}
            </Row>
            <Row label="Category">
              {courses.map((c) => <td key={c.id} className="py-2 px-4 align-top">{c.category || "—"}{c.sub_category ? ` / ${c.sub_category}` : ""}</td>)}
            </Row>
            <Row label="Duration">
              {courses.map((c) => <td key={c.id} className="py-2 px-4 align-top">{formatDuration(c)}</td>)}
            </Row>
            <Row label="Study Mode">
              {courses.map((c) => <td key={c.id} className="py-2 px-4 align-top">{c.study_mode || "—"}</td>)}
            </Row>
            <Row label="Intakes">
              {courses.map((c) => <td key={c.id} className="py-2 px-4 align-top">{c.intakes.join(", ") || "—"}</td>)}
            </Row>
            <Row label="International Fee">
              {courses.map((c) => <td key={c.id} className="py-2 px-4 align-top">{formatFee(c)}</td>)}
            </Row>
            {englishTests.map((t) => (
              <Row key={t} label={`English: ${t}`}>
                {courses.map((c, i) => <td key={c.id} className="py-2 px-4 align-top">{englishMap[i][t] ?? "—"}</td>)}
              </Row>
            ))}
            <Row label="Academic Requirements">
              {courses.map((c) => (
                <td key={c.id} className="py-2 px-4 align-top">
                  {c.academic_requirements.length === 0 ? "—" : (
                    <ul className="space-y-1">
                      {c.academic_requirements.map((a, i) => (
                        <li key={i} className="text-xs">
                          {a.academic_level ?? "—"}
                          {a.academic_score != null ? `: ${a.academic_score} ${a.score_type ?? ""}` : ""}
                          {a.academic_country ? ` (${a.academic_country})` : ""}
                        </li>
                      ))}
                    </ul>
                  )}
                </td>
              ))}
            </Row>
            <Row label="Course Page">
              {courses.map((c) => (
                <td key={c.id} className="py-2 px-4 align-top">
                  {c.course_url ? (
                    <a href={c.course_url} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline inline-flex items-center gap-1">
                      Visit <ExternalLink className="w-3 h-3" />
                    </a>
                  ) : "—"}
                </td>
              ))}
            </Row>
          </tbody>
        </table>
      </div>
    </div>
  );
}
