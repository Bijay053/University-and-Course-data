import { Link, useRoute } from "wouter";
import {
  ArrowLeft, MapPin, Clock, Calendar, DollarSign, Globe2, BookOpen,
  GraduationCap, Languages, Award, ExternalLink, Loader2, Building2,
} from "lucide-react";
import { useGetCourse, getGetCourseQueryKey } from "@workspace/api-client-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

function formatDuration(d: number | null | undefined, term: string | null | undefined) {
  if (d == null) return null;
  const unit = term || "Year";
  return `${d} ${unit}${d !== 1 && !unit.endsWith("s") ? "s" : ""}`;
}

/**
 * Returns a list of [label, value] pairs only for non-null values, so the
 * caller can hide every row that has no data.
 */
function nonNullPairs(...pairs: Array<[string, unknown]>) {
  return pairs.filter(([, v]) => v != null && v !== "" && !(Array.isArray(v) && v.length === 0));
}

export default function CourseDetail() {
  const [, params] = useRoute("/courses/:id");
  const id = params?.id ? parseInt(params.id) : 0;

  const { data: course, isLoading, error } = useGetCourse(id, {
    query: { enabled: !!id, queryKey: getGetCourseQueryKey(id) },
  });

  if (isLoading) {
    return (
      <div className="text-center py-20 text-gray-500 flex items-center justify-center gap-2">
        <Loader2 className="w-5 h-5 animate-spin" /> Loading course…
      </div>
    );
  }

  if (error || !course) {
    return (
      <div className="text-center py-12">
        <p className="text-gray-500">Course not found.</p>
        <Link href="/search">
          <Button variant="outline" className="mt-4"><ArrowLeft className="w-4 h-4 mr-1" /> Back to search</Button>
        </Link>
      </div>
    );
  }

  // The generated client may not include the related arrays in its type; we
  // cast to access them. The API returns them at /api/courses/:id.
  const c = course as unknown as {
    id: number; universityId: number; universityName?: string | null;
    name: string; category?: string | null; subCategory?: string | null;
    courseWebsite?: string | null; duration?: number | null; durationTerm?: string | null;
    studyMode?: string | null; degreeLevel?: string | null; studyLoad?: string | null;
    language?: string | null; description?: string | null; courseStructure?: string | null;
    careerOutcomes?: string | null; otherTest?: string | null; otherTestScore?: string | null;
    otherRequirement?: string | null; deliveryMode?: string | null;
    intakes?: Array<{ id: number; intakeMonth: string | null; intakeYear: number | null; status: string | null }>;
    fees?: Array<{ id: number; internationalFee: number | null; domesticFee: number | null; currency: string | null; feeTerm: string | null; applicationFee: number | null }>;
    englishRequirements?: Array<{ id: number; testType: string; testName: string | null; overall: number | null; listening: number | null; reading: number | null; writing: number | null; speaking: number | null }>;
    academicRequirements?: Array<{ id: number; academicLevel: string | null; academicScore: number | null; scoreType: string | null; academicCountry: string | null }>;
    scholarships?: Array<{ id: number; name: string | null; description: string | null; amount: string | null; criteria: string | null }>;
  };

  const intakes = c.intakes ?? [];
  const fees = c.fees ?? [];
  const eng = c.englishRequirements ?? [];
  const acad = c.academicRequirements ?? [];
  const scholarships = c.scholarships ?? [];

  const overviewRows = nonNullPairs(
    ["Degree Level", c.degreeLevel],
    ["Category", c.category],
    ["Sub-category", c.subCategory],
    ["Study Mode", c.studyMode],
    ["Study Load", c.studyLoad],
    ["Delivery Mode", c.deliveryMode],
    ["Duration", formatDuration(c.duration, c.durationTerm)],
    ["Language", c.language],
    ["Other Test", c.otherTest],
    ["Other Test Score", c.otherTestScore],
    ["Other Requirement", c.otherRequirement],
  );

  const latestFee = fees[fees.length - 1];

  return (
    <div className="space-y-4">
      <Link href="/search">
        <Button variant="outline" size="sm"><ArrowLeft className="w-4 h-4 mr-1" /> Back to search</Button>
      </Link>

      {/* Hero */}
      <div className="bg-gradient-to-r from-[#0F172A] to-[#DC2626] rounded-2xl p-6 text-white shadow-lg">
        <Badge className="bg-white/20 text-white hover:bg-white/30 mb-2">
          <BookOpen className="w-3 h-3 mr-1" />Course
        </Badge>
        <h1 className="text-3xl font-bold">{c.name}</h1>
        {c.universityName && (
          <Link href={`/universities/${c.universityId}`}>
            <p className="text-blue-100 mt-2 hover:text-white cursor-pointer inline-flex items-center gap-1">
              <Building2 className="w-4 h-4" /> {c.universityName}
            </p>
          </Link>
        )}
        <div className="flex flex-wrap gap-x-6 gap-y-2 mt-4 text-sm">
          {c.degreeLevel && <span className="flex items-center gap-1"><GraduationCap className="w-4 h-4" /> {c.degreeLevel}</span>}
          {formatDuration(c.duration, c.durationTerm) && <span className="flex items-center gap-1"><Clock className="w-4 h-4" /> {formatDuration(c.duration, c.durationTerm)}</span>}
          {latestFee?.internationalFee != null && (
            <span className="flex items-center gap-1">
              <DollarSign className="w-4 h-4" /> {latestFee.currency || "AUD"} {Math.round(latestFee.internationalFee).toLocaleString()}{latestFee.feeTerm ? ` / ${latestFee.feeTerm}` : ""}
            </span>
          )}
          {c.language && <span className="flex items-center gap-1"><Languages className="w-4 h-4" /> {c.language}</span>}
        </div>
        {c.courseWebsite && (
          <a href={c.courseWebsite} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 mt-4 text-white bg-white/15 hover:bg-white/25 px-3 py-1.5 rounded-md text-sm">
            <Globe2 className="w-4 h-4" /> Visit official course page <ExternalLink className="w-3 h-3" />
          </a>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 space-y-4">
          {/* Description */}
          {c.description && (
            <Card>
              <CardHeader><CardTitle>About this course</CardTitle></CardHeader>
              <CardContent>
                <p className="whitespace-pre-line text-sm text-gray-700">{c.description}</p>
              </CardContent>
            </Card>
          )}

          {/* Course Structure */}
          {c.courseStructure && (
            <Card>
              <CardHeader><CardTitle>Course structure</CardTitle></CardHeader>
              <CardContent>
                <p className="whitespace-pre-line text-sm text-gray-700">{c.courseStructure}</p>
              </CardContent>
            </Card>
          )}

          {/* Career Outcomes */}
          {c.careerOutcomes && (
            <Card>
              <CardHeader><CardTitle>Career outcomes</CardTitle></CardHeader>
              <CardContent>
                <p className="whitespace-pre-line text-sm text-gray-700">{c.careerOutcomes}</p>
              </CardContent>
            </Card>
          )}

          {/* Overview / Details */}
          {overviewRows.length > 0 && (
            <Card>
              <CardHeader><CardTitle>Details</CardTitle></CardHeader>
              <CardContent>
                <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3">
                  {overviewRows.map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-xs text-gray-500 uppercase tracking-wide">{label}</dt>
                      <dd className="text-sm font-medium text-gray-900 mt-0.5">{String(value)}</dd>
                    </div>
                  ))}
                </dl>
              </CardContent>
            </Card>
          )}

          {/* English Requirements */}
          {eng.length > 0 && (
            <Card>
              <CardHeader><CardTitle className="flex items-center gap-2"><Award className="w-5 h-5" /> English Requirements</CardTitle></CardHeader>
              <CardContent>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-gray-500 uppercase border-b">
                      <th className="text-left py-2">Test</th>
                      <th className="text-left py-2">Overall</th>
                      <th className="text-left py-2">Listening</th>
                      <th className="text-left py-2">Reading</th>
                      <th className="text-left py-2">Writing</th>
                      <th className="text-left py-2">Speaking</th>
                    </tr>
                  </thead>
                  <tbody>
                    {eng.map((e) => (
                      <tr key={e.id} className="border-b last:border-0">
                        <td className="py-2 font-medium">{e.testName || e.testType}</td>
                        <td className="py-2">{e.overall ?? <span className="text-gray-300">—</span>}</td>
                        <td className="py-2">{e.listening ?? <span className="text-gray-300">—</span>}</td>
                        <td className="py-2">{e.reading ?? <span className="text-gray-300">—</span>}</td>
                        <td className="py-2">{e.writing ?? <span className="text-gray-300">—</span>}</td>
                        <td className="py-2">{e.speaking ?? <span className="text-gray-300">—</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          )}

          {/* Academic Requirements */}
          {acad.length > 0 && (
            <Card>
              <CardHeader><CardTitle>Academic Requirements</CardTitle></CardHeader>
              <CardContent>
                <ul className="space-y-2">
                  {acad.map((a) => (
                    <li key={a.id} className="text-sm border-l-2 border-red-200 pl-3">
                      <strong>{a.academicLevel ?? "Requirement"}</strong>
                      {a.academicScore != null && <>: <span className="text-red-700">{a.academicScore} {a.scoreType ?? ""}</span></>}
                      {a.academicCountry && <span className="text-gray-500"> ({a.academicCountry})</span>}
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}

          {/* Scholarships */}
          {scholarships.length > 0 && (
            <Card>
              <CardHeader><CardTitle>Scholarships</CardTitle></CardHeader>
              <CardContent>
                <ul className="space-y-3">
                  {scholarships.map((s) => (
                    <li key={s.id} className="border rounded-lg p-3">
                      <div className="font-semibold text-sm">{s.name ?? "Scholarship"}</div>
                      {s.amount && <div className="text-xs text-emerald-700 mt-0.5">{s.amount}</div>}
                      {s.description && <p className="text-xs text-gray-600 mt-1">{s.description}</p>}
                      {s.criteria && <p className="text-xs text-gray-500 mt-1"><strong>Criteria:</strong> {s.criteria}</p>}
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </div>

        {/* Side panel */}
        <div className="space-y-4">
          {/* Fees */}
          {fees.length > 0 && (
            <Card>
              <CardHeader><CardTitle className="flex items-center gap-2"><DollarSign className="w-5 h-5" /> Fees</CardTitle></CardHeader>
              <CardContent className="space-y-3 text-sm">
                {fees.map((f) => (
                  <div key={f.id} className="border-b last:border-0 pb-3 last:pb-0 space-y-1">
                    {f.internationalFee != null && (
                      <div className="flex justify-between"><span className="text-gray-600">International</span><strong>{f.currency || "AUD"} {Math.round(f.internationalFee).toLocaleString()}{f.feeTerm ? ` / ${f.feeTerm}` : ""}</strong></div>
                    )}
                    {f.domesticFee != null && (
                      <div className="flex justify-between"><span className="text-gray-600">Domestic</span><strong>{f.currency || "AUD"} {Math.round(f.domesticFee).toLocaleString()}{f.feeTerm ? ` / ${f.feeTerm}` : ""}</strong></div>
                    )}
                    {f.applicationFee != null && (
                      <div className="flex justify-between"><span className="text-gray-600">Application</span><strong>{f.currency || "AUD"} {f.applicationFee}</strong></div>
                    )}
                  </div>
                ))}
              </CardContent>
            </Card>
          )}

          {/* Intakes */}
          {intakes.length > 0 && (
            <Card>
              <CardHeader><CardTitle className="flex items-center gap-2"><Calendar className="w-5 h-5" /> Intakes</CardTitle></CardHeader>
              <CardContent>
                <div className="flex flex-wrap gap-2">
                  {intakes.map((i) => (
                    <Badge key={i.id} variant="secondary">
                      {i.intakeMonth ?? "—"}{i.intakeYear ? ` ${i.intakeYear}` : ""}
                    </Badge>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}

          {/* Quick actions */}
          <Card>
            <CardContent className="pt-6 space-y-2">
              <Link href={`/universities/${c.universityId}`}>
                <Button variant="outline" className="w-full">
                  <Building2 className="w-4 h-4 mr-1" /> View University
                </Button>
              </Link>
              {c.courseWebsite && (
                <a href={c.courseWebsite} target="_blank" rel="noreferrer">
                  <Button className="w-full bg-red-600 hover:bg-red-700">
                    <Globe2 className="w-4 h-4 mr-1" /> Official Page
                  </Button>
                </a>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
