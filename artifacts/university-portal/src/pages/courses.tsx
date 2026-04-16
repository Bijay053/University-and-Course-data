import { useState } from "react";
import { Link } from "wouter";
import { useListCourses } from "@workspace/api-client-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Plus, Search, ChevronLeft, ChevronRight, X, ExternalLink } from "lucide-react";
import { CATEGORY_NAMES, DEGREE_LEVELS, STUDY_MODES, getSubCategories } from "@/lib/course-constants";

const DEGREE_COLORS: Record<string, string> = {
  "Bachelor": "bg-blue-100 text-blue-800",
  "Master": "bg-purple-100 text-purple-800",
  "Doctor/Doctorate": "bg-red-100 text-red-800",
  "Certificate & Diploma": "bg-green-100 text-green-800",
  "Graduate Certificate & Diploma": "bg-teal-100 text-teal-800",
  "Associate Degree or Equivalent": "bg-orange-100 text-orange-800",
  "Pathway to Undergraduate": "bg-yellow-100 text-yellow-800",
  "English Language": "bg-gray-100 text-gray-700",
  "Bachelor Dual Degree": "bg-blue-100 text-blue-800",
  "Master Dual Degree": "bg-purple-100 text-purple-800",
  "Dual Degree": "bg-indigo-100 text-indigo-800",
};

const ALL = "__all__";

function Cell({ v, className = "" }: { v: unknown; className?: string }) {
  if (v == null || v === "") return <span className="text-gray-300">—</span>;
  return <span className={className}>{String(v)}</span>;
}

function ScoreCell({ v }: { v: unknown }) {
  if (v == null || v === "") return <span className="text-gray-300">—</span>;
  return <span className="font-mono text-xs font-semibold">{String(v)}</span>;
}

export default function Courses() {
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState(ALL);
  const [subCategory, setSubCategory] = useState(ALL);
  const [degreeLevel, setDegreeLevel] = useState(ALL);
  const [studyMode, setStudyMode] = useState(ALL);
  const [page, setPage] = useState(1);
  const limit = 20;

  const subCategories = category !== ALL ? getSubCategories(category) : [];

  const { data, isLoading } = useListCourses({
    search: search || undefined,
    category: category !== ALL ? category : undefined,
    subCategory: subCategory !== ALL ? subCategory : undefined,
    degreeLevel: degreeLevel !== ALL ? degreeLevel : undefined,
    studyMode: studyMode !== ALL ? studyMode : undefined,
    page,
    limit,
  });

  const courses = data?.data ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.ceil(total / limit);
  const hasFilters = category !== ALL || subCategory !== ALL || degreeLevel !== ALL || studyMode !== ALL || !!search;

  function clearFilters() {
    setSearch(""); setCategory(ALL); setSubCategory(ALL);
    setDegreeLevel(ALL); setStudyMode(ALL); setPage(1);
  }

  function handleCategoryChange(val: string) {
    setCategory(val); setSubCategory(ALL); setPage(1);
  }

  const colClass = "px-3 py-2 text-xs whitespace-nowrap border-r border-gray-100 last:border-r-0";
  const headClass = `${colClass} font-semibold text-gray-500 bg-gray-50 sticky top-0 z-10`;

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row justify-between gap-4 items-start sm:items-center">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Courses</h1>
          <p className="text-muted-foreground text-sm">Manage academic programs across all universities.</p>
        </div>
        <Link href="/courses/new">
          <Button size="sm"><Plus className="mr-2 h-4 w-4" /> Add Course</Button>
        </Link>
      </div>

      <Card>
        <CardHeader className="pb-3 pt-4 space-y-2">
          <div className="flex flex-wrap gap-2 items-center">
            <div className="flex items-center gap-1.5 flex-1 min-w-[160px]">
              <Search className="w-4 h-4 text-muted-foreground flex-shrink-0" />
              <Input
                placeholder="Search courses..."
                value={search}
                onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                className="border-0 focus-visible:ring-0 px-0 h-8 text-sm"
              />
            </div>
            <Select value={category} onValueChange={handleCategoryChange}>
              <SelectTrigger className="w-[190px] h-8 text-xs">
                <SelectValue placeholder="All Categories" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All Categories</SelectItem>
                {CATEGORY_NAMES.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
              </SelectContent>
            </Select>
            {subCategories.length > 0 && (
              <Select value={subCategory} onValueChange={(v) => { setSubCategory(v); setPage(1); }}>
                <SelectTrigger className="w-[190px] h-8 text-xs">
                  <SelectValue placeholder="All Sub-categories" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All Sub-categories</SelectItem>
                  {subCategories.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                </SelectContent>
              </Select>
            )}
            <Select value={degreeLevel} onValueChange={(v) => { setDegreeLevel(v); setPage(1); }}>
              <SelectTrigger className="w-[190px] h-8 text-xs">
                <SelectValue placeholder="All Levels" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All Levels</SelectItem>
                {DEGREE_LEVELS.map((l) => <SelectItem key={l} value={l}>{l}</SelectItem>)}
              </SelectContent>
            </Select>
            <Select value={studyMode} onValueChange={(v) => { setStudyMode(v); setPage(1); }}>
              <SelectTrigger className="w-[130px] h-8 text-xs">
                <SelectValue placeholder="All Modes" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All Modes</SelectItem>
                {STUDY_MODES.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
              </SelectContent>
            </Select>
            {hasFilters && (
              <Button variant="ghost" size="sm" onClick={clearFilters} className="h-8 text-muted-foreground text-xs">
                <X className="h-3 w-3 mr-1" /> Clear
              </Button>
            )}
          </div>
          {total > 0 && (
            <p className="text-xs text-muted-foreground">{total} course{total !== 1 ? "s" : ""} found</p>
          )}
        </CardHeader>

        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-xs border-collapse min-w-[2400px]">
              <thead>
                <tr className="border-b border-gray-200">
                  {/* University & Course Info */}
                  <th className={`${headClass} min-w-[200px] sticky left-0 z-20 bg-gray-50`}>University</th>
                  <th className={`${headClass} min-w-[260px] sticky left-[200px] z-20 bg-gray-50`}>Course Name</th>
                  <th className={`${headClass} min-w-[160px]`}>Category</th>
                  <th className={`${headClass} min-w-[160px]`}>Sub Category</th>
                  <th className={`${headClass} min-w-[100px]`}>Website</th>
                  <th className={`${headClass} min-w-[80px]`}>Duration</th>
                  <th className={`${headClass} min-w-[90px]`}>Term</th>
                  <th className={`${headClass} min-w-[90px]`}>Study Mode</th>
                  <th className={`${headClass} min-w-[140px]`}>Degree Level</th>
                  <th className={`${headClass} min-w-[120px]`}>City</th>
                  {/* Intakes */}
                  <th className={`${headClass} min-w-[120px] bg-blue-50`}>Intake Month</th>
                  <th className={`${headClass} min-w-[80px] bg-blue-50`}>Intake Day</th>
                  {/* Fees */}
                  <th className={`${headClass} min-w-[100px] bg-green-50`}>Int'l Fee</th>
                  <th className={`${headClass} min-w-[90px] bg-green-50`}>Fee Term</th>
                  <th className={`${headClass} min-w-[80px] bg-green-50`}>Fee Year</th>
                  <th className={`${headClass} min-w-[70px] bg-green-50`}>Currency</th>
                  {/* Study Details */}
                  <th className={`${headClass} min-w-[90px]`}>Study Load</th>
                  <th className={`${headClass} min-w-[80px]`}>Language</th>
                  {/* IELTS */}
                  <th className={`${headClass} min-w-[60px] bg-amber-50`}>IELTS L</th>
                  <th className={`${headClass} min-w-[60px] bg-amber-50`}>IELTS S</th>
                  <th className={`${headClass} min-w-[60px] bg-amber-50`}>IELTS W</th>
                  <th className={`${headClass} min-w-[60px] bg-amber-50`}>IELTS R</th>
                  <th className={`${headClass} min-w-[60px] bg-amber-50`}>IELTS O</th>
                  {/* PTE */}
                  <th className={`${headClass} min-w-[60px] bg-violet-50`}>PTE L</th>
                  <th className={`${headClass} min-w-[60px] bg-violet-50`}>PTE S</th>
                  <th className={`${headClass} min-w-[60px] bg-violet-50`}>PTE W</th>
                  <th className={`${headClass} min-w-[60px] bg-violet-50`}>PTE R</th>
                  <th className={`${headClass} min-w-[60px] bg-violet-50`}>PTE O</th>
                  {/* TOEFL */}
                  <th className={`${headClass} min-w-[60px] bg-rose-50`}>TOEFL L</th>
                  <th className={`${headClass} min-w-[60px] bg-rose-50`}>TOEFL S</th>
                  <th className={`${headClass} min-w-[60px] bg-rose-50`}>TOEFL W</th>
                  <th className={`${headClass} min-w-[60px] bg-rose-50`}>TOEFL R</th>
                  <th className={`${headClass} min-w-[60px] bg-rose-50`}>TOEFL O</th>
                  {/* Other English Test */}
                  <th className={`${headClass} min-w-[100px] bg-pink-50`}>Other Test</th>
                  <th className={`${headClass} min-w-[60px] bg-pink-50`}>OT R</th>
                  <th className={`${headClass} min-w-[60px] bg-pink-50`}>OT L</th>
                  <th className={`${headClass} min-w-[60px] bg-pink-50`}>OT S</th>
                  <th className={`${headClass} min-w-[60px] bg-pink-50`}>OT W</th>
                  <th className={`${headClass} min-w-[60px] bg-pink-50`}>OT O</th>
                  {/* Academic Req */}
                  <th className={`${headClass} min-w-[120px] bg-cyan-50`}>Academic Level</th>
                  <th className={`${headClass} min-w-[80px] bg-cyan-50`}>Acad. Score</th>
                  <th className={`${headClass} min-w-[100px] bg-cyan-50`}>Score Type</th>
                  <th className={`${headClass} min-w-[100px] bg-cyan-50`}>Acad. Country</th>
                  {/* Additional */}
                  <th className={`${headClass} min-w-[90px]`}>Other Req.</th>
                  {/* Scholarship */}
                  <th className={`${headClass} min-w-[160px] bg-yellow-50`}>Scholarship</th>
                  <th className={`${headClass} min-w-[140px] bg-yellow-50`}>Eligibility</th>
                  {/* Actions */}
                  <th className={`${headClass} min-w-[60px] sticky right-0 z-20 bg-gray-50`}>View</th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  <tr>
                    <td colSpan={50} className="text-center py-10 text-muted-foreground text-sm">
                      Loading courses...
                    </td>
                  </tr>
                ) : courses.length === 0 ? (
                  <tr>
                    <td colSpan={50} className="text-center py-10 text-muted-foreground text-sm">
                      No courses found
                    </td>
                  </tr>
                ) : (
                  courses.map((course, i) => {
                    const row = course as Record<string, unknown>;
                    const rowBg = i % 2 === 0 ? "bg-white" : "bg-gray-50/50";
                    return (
                      <tr key={course.id} className={`${rowBg} hover:bg-blue-50/30 border-b border-gray-100 group`}>
                        {/* University */}
                        <td className={`${colClass} sticky left-0 z-10 ${rowBg} group-hover:bg-blue-50/30 font-medium max-w-[200px]`}>
                          <span className="truncate block max-w-[196px]">{row.universityName as string || "—"}</span>
                        </td>
                        {/* Course Name */}
                        <td className={`${colClass} sticky left-[200px] z-10 ${rowBg} group-hover:bg-blue-50/30 font-medium`}>
                          <Link href={`/courses/${course.id}`} className="hover:underline text-blue-700 block max-w-[256px] line-clamp-2">
                            {course.name}
                          </Link>
                        </td>
                        {/* Category */}
                        <td className={colClass}><Cell v={course.category} /></td>
                        {/* Sub Category */}
                        <td className={colClass}><Cell v={course.subCategory} /></td>
                        {/* Website */}
                        <td className={colClass}>
                          {row.courseWebsite ? (
                            <a href={row.courseWebsite as string} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline flex items-center gap-1">
                              Link <ExternalLink className="h-3 w-3" />
                            </a>
                          ) : <span className="text-gray-300">—</span>}
                        </td>
                        {/* Duration */}
                        <td className={colClass}><Cell v={course.duration} /></td>
                        {/* Duration Term */}
                        <td className={colClass}><Cell v={course.durationTerm} /></td>
                        {/* Study Mode */}
                        <td className={colClass}><Cell v={course.studyMode} /></td>
                        {/* Degree Level */}
                        <td className={colClass}>
                          {course.degreeLevel ? (
                            <span className={`inline-flex px-1.5 py-0.5 rounded text-xs font-medium ${DEGREE_COLORS[course.degreeLevel] ?? "bg-gray-100 text-gray-700"}`}>
                              {course.degreeLevel}
                            </span>
                          ) : <span className="text-gray-300">—</span>}
                        </td>
                        {/* City */}
                        <td className={colClass}><Cell v={row.city} /></td>
                        {/* Intakes */}
                        <td className={`${colClass} bg-blue-50/40`}><Cell v={row.intakeMonths} /></td>
                        <td className={`${colClass} bg-blue-50/40`}><Cell v={row.intakeDays} /></td>
                        {/* Fees */}
                        <td className={`${colClass} bg-green-50/40`}><ScoreCell v={row.internationalFee} /></td>
                        <td className={`${colClass} bg-green-50/40`}><Cell v={row.feeTerm} /></td>
                        <td className={`${colClass} bg-green-50/40`}><Cell v={row.feeYear} /></td>
                        <td className={`${colClass} bg-green-50/40`}><Cell v={row.currency} /></td>
                        {/* Study Details */}
                        <td className={colClass}><Cell v={course.studyLoad} /></td>
                        <td className={colClass}><Cell v={course.language} /></td>
                        {/* IELTS */}
                        <td className={`${colClass} bg-amber-50/40`}><ScoreCell v={row.ieltsListening} /></td>
                        <td className={`${colClass} bg-amber-50/40`}><ScoreCell v={row.ieltsSpeaking} /></td>
                        <td className={`${colClass} bg-amber-50/40`}><ScoreCell v={row.ieltsWriting} /></td>
                        <td className={`${colClass} bg-amber-50/40`}><ScoreCell v={row.ieltsReading} /></td>
                        <td className={`${colClass} bg-amber-50/40`}><ScoreCell v={row.ieltsOverall} /></td>
                        {/* PTE */}
                        <td className={`${colClass} bg-violet-50/40`}><ScoreCell v={row.pteListening} /></td>
                        <td className={`${colClass} bg-violet-50/40`}><ScoreCell v={row.pteSpeaking} /></td>
                        <td className={`${colClass} bg-violet-50/40`}><ScoreCell v={row.pteWriting} /></td>
                        <td className={`${colClass} bg-violet-50/40`}><ScoreCell v={row.pteReading} /></td>
                        <td className={`${colClass} bg-violet-50/40`}><ScoreCell v={row.pteOverall} /></td>
                        {/* TOEFL */}
                        <td className={`${colClass} bg-rose-50/40`}><ScoreCell v={row.toeflListening} /></td>
                        <td className={`${colClass} bg-rose-50/40`}><ScoreCell v={row.toeflSpeaking} /></td>
                        <td className={`${colClass} bg-rose-50/40`}><ScoreCell v={row.toeflWriting} /></td>
                        <td className={`${colClass} bg-rose-50/40`}><ScoreCell v={row.toeflReading} /></td>
                        <td className={`${colClass} bg-rose-50/40`}><ScoreCell v={row.toeflOverall} /></td>
                        {/* Other English Test */}
                        <td className={`${colClass} bg-pink-50/40`}><Cell v={row.otherEnglishTestName} /></td>
                        <td className={`${colClass} bg-pink-50/40`}><ScoreCell v={row.otherEnglishReading} /></td>
                        <td className={`${colClass} bg-pink-50/40`}><ScoreCell v={row.otherEnglishListening} /></td>
                        <td className={`${colClass} bg-pink-50/40`}><ScoreCell v={row.otherEnglishSpeaking} /></td>
                        <td className={`${colClass} bg-pink-50/40`}><ScoreCell v={row.otherEnglishWriting} /></td>
                        <td className={`${colClass} bg-pink-50/40`}><ScoreCell v={row.otherEnglishOverall} /></td>
                        {/* Academic */}
                        <td className={`${colClass} bg-cyan-50/40`}><Cell v={row.academicLevel} /></td>
                        <td className={`${colClass} bg-cyan-50/40`}><ScoreCell v={row.academicScore} /></td>
                        <td className={`${colClass} bg-cyan-50/40`}><Cell v={row.scoreType} /></td>
                        <td className={`${colClass} bg-cyan-50/40`}><Cell v={row.academicCountry} /></td>
                        {/* Additional */}
                        <td className={colClass}><Cell v={course.otherRequirement} /></td>
                        {/* Scholarship */}
                        <td className={`${colClass} bg-yellow-50/40 max-w-[160px]`}>
                          <span className="line-clamp-2">{(row.scholarshipDetails as string) || <span className="text-gray-300">—</span>}</span>
                        </td>
                        <td className={`${colClass} bg-yellow-50/40 max-w-[140px]`}>
                          <span className="line-clamp-2">{(row.scholarshipEligibility as string) || <span className="text-gray-300">—</span>}</span>
                        </td>
                        {/* View */}
                        <td className={`${colClass} sticky right-0 z-10 ${rowBg} group-hover:bg-blue-50/30 text-center`}>
                          <Link href={`/courses/${course.id}`}>
                            <Button variant="ghost" size="sm" className="h-6 px-2 text-xs">View</Button>
                          </Link>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 border-t bg-white">
              <p className="text-xs text-muted-foreground">
                Showing {(page - 1) * limit + 1}–{Math.min(page * limit, total)} of {total}
              </p>
              <div className="flex items-center gap-2">
                <Button variant="outline" size="sm" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}>
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <span className="text-xs">Page {page} of {totalPages}</span>
                <Button variant="outline" size="sm" onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page === totalPages}>
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Column legend */}
      <div className="flex flex-wrap gap-3 text-xs text-muted-foreground px-1">
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-blue-100 inline-block" /> Intakes</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-green-100 inline-block" /> Fees</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-amber-100 inline-block" /> IELTS</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-violet-100 inline-block" /> PTE</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-rose-100 inline-block" /> TOEFL</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-pink-100 inline-block" /> Other English</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-cyan-100 inline-block" /> Academic Req.</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-yellow-100 inline-block" /> Scholarship</span>
      </div>
    </div>
  );
}
