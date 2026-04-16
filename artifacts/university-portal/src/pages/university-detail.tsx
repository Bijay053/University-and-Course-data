import { useState } from "react";
import { useRoute, Link } from "wouter";
import { useGetUniversity, getGetUniversityQueryKey, useListCourses } from "@workspace/api-client-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import {
  Building2, MapPin, Globe, Search, ChevronLeft, ChevronRight, X,
  BookOpen, Languages, GraduationCap, Award, ExternalLink,
} from "lucide-react";
import { CATEGORY_NAMES, DEGREE_LEVELS, STUDY_MODES, getSubCategories } from "@/lib/course-constants";

const ALL = "__all__";

type Tab = "courses" | "english" | "academic" | "scholarships";

const DEGREE_COLORS: Record<string, string> = {
  Bachelor: "bg-blue-100 text-blue-700",
  Master: "bg-purple-100 text-purple-700",
  "PhD": "bg-red-100 text-red-700",
  "Doctor/Doctorate": "bg-red-100 text-red-700",
  "Certificate & Diploma": "bg-green-100 text-green-700",
  "Graduate Certificate & Diploma": "bg-teal-100 text-teal-700",
  "Associate Degree or Equivalent": "bg-orange-100 text-orange-700",
};

function num(v: number | null | undefined) {
  return v != null ? v : "—";
}
function txt(v: string | null | undefined) {
  return v || "—";
}

export default function UniversityDetail() {
  const [, params] = useRoute("/universities/:id");
  const id = params?.id ? parseInt(params.id) : 0;

  const [tab, setTab] = useState<Tab>("courses");

  // Courses tab state
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState(ALL);
  const [subCategory, setSubCategory] = useState(ALL);
  const [degreeLevel, setDegreeLevel] = useState(ALL);
  const [studyMode, setStudyMode] = useState(ALL);
  const [page, setPage] = useState(1);
  const limit = 50;

  const subCategories = category !== ALL ? getSubCategories(category) : [];

  const { data: uni, isLoading: uniLoading } = useGetUniversity(id, {
    query: { enabled: !!id, queryKey: getGetUniversityQueryKey(id) },
  });

  // Filtered + paginated for Courses tab
  const { data: coursesData, isLoading: coursesLoading } = useListCourses(
    {
      universityId: id,
      search: search || undefined,
      category: category !== ALL ? category : undefined,
      subCategory: subCategory !== ALL ? subCategory : undefined,
      degreeLevel: degreeLevel !== ALL ? degreeLevel : undefined,
      studyMode: studyMode !== ALL ? studyMode : undefined,
      page,
      limit,
    },
    { query: { enabled: !!id && tab === "courses" } },
  );

  // All courses for the other tabs (no pagination)
  const { data: allCoursesData } = useListCourses(
    { universityId: id, limit: 500 },
    { query: { enabled: !!id && tab !== "courses" } },
  );

  const courses = coursesData?.data ?? [];
  const total = coursesData?.total ?? 0;
  const totalPages = Math.ceil(total / limit);
  const hasFilters = category !== ALL || subCategory !== ALL || degreeLevel !== ALL || studyMode !== ALL || search;

  const allCourses = allCoursesData?.data ?? [];

  const englishCourses = allCourses.filter(
    (c) => c.ieltsOverall || c.pteOverall || c.toeflOverall ||
      c.ieltsListening || c.pteListening || c.toeflListening,
  );
  const academicCourses = allCourses.filter(
    (c) => c.academicLevel || c.academicScore || c.academicCountry,
  );
  const scholarshipCourses = allCourses.filter(
    (c) => c.scholarshipDetails,
  );

  function clearFilters() {
    setSearch(""); setCategory(ALL); setSubCategory(ALL); setDegreeLevel(ALL); setStudyMode(ALL); setPage(1);
  }
  function handleCategoryChange(val: string) { setCategory(val); setSubCategory(ALL); setPage(1); }

  const TABS: { key: Tab; label: string; icon: React.ReactNode; count?: number }[] = [
    { key: "courses", label: "Courses", icon: <BookOpen className="w-4 h-4" />, count: uni ? total : undefined },
    { key: "english", label: "English Proficiency", icon: <Languages className="w-4 h-4" /> },
    { key: "academic", label: "Academic Requirements", icon: <GraduationCap className="w-4 h-4" /> },
    { key: "scholarships", label: "Scholarships", icon: <Award className="w-4 h-4" /> },
  ];

  if (uniLoading) return <div className="py-16 text-center text-muted-foreground">Loading...</div>;
  if (!uni) return <div className="py-16 text-center text-muted-foreground">University not found</div>;

  return (
    <div className="space-y-6">
      {/* University Header */}
      <div className="flex items-start gap-4">
        <div className="h-14 w-14 bg-primary/10 rounded-xl flex items-center justify-center shrink-0">
          <Building2 className="h-7 w-7 text-primary" />
        </div>
        <div className="min-w-0">
          <h1 className="text-2xl font-bold tracking-tight">{uni.name}</h1>
          <div className="flex flex-wrap items-center gap-4 text-muted-foreground mt-1">
            <span className="flex items-center gap-1 text-sm">
              <MapPin className="h-4 w-4 shrink-0" /> {uni.city}, {uni.country}
            </span>
            {uni.website && (
              <a href={uni.website} target="_blank" rel="noreferrer"
                className="flex items-center gap-1 text-sm hover:underline text-primary">
                <Globe className="h-4 w-4 shrink-0" />{uni.website}
              </a>
            )}
          </div>
          {uni.description && (
            <p className="text-sm text-muted-foreground mt-2 max-w-2xl">{uni.description}</p>
          )}
        </div>
      </div>

      {/* Tab Bar */}
      <div className="border-b flex gap-0">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex items-center gap-2 px-5 py-3 text-sm font-medium border-b-2 transition-colors -mb-px ${
              tab === t.key
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground hover:border-gray-200"
            }`}
          >
            {t.icon}
            {t.label}
            {t.key === "courses" && total > 0 && (
              <span className="ml-1 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full font-semibold">
                {total}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* ── COURSES TAB ── */}
      {tab === "courses" && (
        <div className="space-y-3">
          {/* Filters */}
          <div className="flex flex-wrap gap-2 items-center">
            <div className="flex items-center gap-1.5 border rounded-md px-2 h-9 flex-1 min-w-[180px] max-w-xs bg-white">
              <Search className="h-4 w-4 text-muted-foreground shrink-0" />
              <Input
                placeholder="Search courses..."
                value={search}
                onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                className="border-0 focus-visible:ring-0 px-0 h-8 bg-transparent"
              />
            </div>
            <Select value={category} onValueChange={handleCategoryChange}>
              <SelectTrigger className="w-[180px] h-9 text-sm"><SelectValue placeholder="All Categories" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All Categories</SelectItem>
                {CATEGORY_NAMES.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
              </SelectContent>
            </Select>
            {subCategories.length > 0 && (
              <Select value={subCategory} onValueChange={(v) => { setSubCategory(v); setPage(1); }}>
                <SelectTrigger className="w-[180px] h-9 text-sm"><SelectValue placeholder="All Sub-cats" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All Sub-categories</SelectItem>
                  {subCategories.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                </SelectContent>
              </Select>
            )}
            <Select value={degreeLevel} onValueChange={(v) => { setDegreeLevel(v); setPage(1); }}>
              <SelectTrigger className="w-[150px] h-9 text-sm"><SelectValue placeholder="All Levels" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All Levels</SelectItem>
                {DEGREE_LEVELS.map((l) => <SelectItem key={l} value={l}>{l}</SelectItem>)}
              </SelectContent>
            </Select>
            <Select value={studyMode} onValueChange={(v) => { setStudyMode(v); setPage(1); }}>
              <SelectTrigger className="w-[130px] h-9 text-sm"><SelectValue placeholder="Mode" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All Modes</SelectItem>
                {STUDY_MODES.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
              </SelectContent>
            </Select>
            {hasFilters && (
              <Button variant="ghost" size="sm" onClick={clearFilters} className="h-9 text-muted-foreground">
                <X className="h-3.5 w-3.5 mr-1" />Clear
              </Button>
            )}
            <span className="ml-auto text-sm text-muted-foreground">{total} course{total !== 1 ? "s" : ""}</span>
          </div>

          {/* Wide scrollable table */}
          <div className="border rounded-xl overflow-auto" style={{ maxHeight: "70vh" }}>
            <table className="text-xs whitespace-nowrap border-collapse" style={{ minWidth: 3000 }}>
              <thead className="bg-gray-50 sticky top-0 z-20">
                {/* Group headers */}
                <tr className="text-[10px] font-bold text-gray-500 uppercase tracking-wide border-b">
                  <th className="sticky left-0 z-30 bg-gray-50 border-r px-3 py-2 text-left" colSpan={2}>Course</th>
                  <th className="px-2 py-2 border-r text-center" colSpan={7} style={{ background: "#f0fdf4", color: "#15803d" }}>Details</th>
                  <th className="px-2 py-2 border-r text-center" colSpan={3} style={{ background: "#eff6ff", color: "#1d4ed8" }}>Intake</th>
                  <th className="px-2 py-2 border-r text-center" colSpan={4} style={{ background: "#fefce8", color: "#a16207" }}>Fee</th>
                  <th className="px-2 py-2 border-r text-center" colSpan={5} style={{ background: "#fdf4ff", color: "#7e22ce" }}>IELTS</th>
                  <th className="px-2 py-2 border-r text-center" colSpan={5} style={{ background: "#fff7ed", color: "#c2410c" }}>PTE</th>
                  <th className="px-2 py-2 border-r text-center" colSpan={5} style={{ background: "#fef2f2", color: "#be123c" }}>TOEFL</th>
                  <th className="px-2 py-2 border-r text-center" colSpan={6} style={{ background: "#fdf2f8", color: "#be185d" }}>Other English</th>
                  <th className="px-2 py-2 border-r text-center" colSpan={4} style={{ background: "#ecfeff", color: "#0e7490" }}>Academic Req.</th>
                  <th className="px-2 py-2 text-center" colSpan={2} style={{ background: "#fefce8", color: "#a16207" }}>Other</th>
                </tr>
                {/* Column headers */}
                <tr className="border-b bg-gray-50">
                  {/* Sticky cols */}
                  <th className="sticky left-0 z-30 bg-gray-50 border-r px-3 py-2 text-left font-semibold text-gray-700 min-w-[220px]">Course Name</th>
                  <th className="sticky bg-gray-50 border-r px-2 py-2 text-left font-semibold text-gray-700 min-w-[80px]" style={{ left: 220, zIndex: 29 }}>Category</th>
                  {/* Details */}
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[100px]">Sub Category</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px]">Website</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[70px]">Duration</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px]">Term</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[80px]">Study Mode</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[120px] border-r">Degree Level</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px]">Study Load</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px] border-r">Language</th>
                  {/* Intake */}
                  <th className="px-2 py-2 text-blue-700 font-medium min-w-[90px]">Month</th>
                  <th className="px-2 py-2 text-blue-700 font-medium min-w-[50px]">Day</th>
                  <th className="px-2 py-2 text-blue-700 font-medium min-w-[40px] border-r">City</th>
                  {/* Fee */}
                  <th className="px-2 py-2 text-amber-700 font-medium min-w-[70px]">Int'l Fee</th>
                  <th className="px-2 py-2 text-amber-700 font-medium min-w-[60px]">Fee Term</th>
                  <th className="px-2 py-2 text-amber-700 font-medium min-w-[50px]">Year</th>
                  <th className="px-2 py-2 text-amber-700 font-medium min-w-[55px] border-r">Currency</th>
                  {/* IELTS */}
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px]">L</th>
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px]">S</th>
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px]">W</th>
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px]">R</th>
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px] border-r">O</th>
                  {/* PTE */}
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px]">L</th>
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px]">S</th>
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px]">W</th>
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px]">R</th>
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px] border-r">O</th>
                  {/* TOEFL */}
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px]">L</th>
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px]">S</th>
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px]">W</th>
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px]">R</th>
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px] border-r">O</th>
                  {/* Other English */}
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[80px]">Other Test</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px]">R</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px]">L</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px]">S</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px]">W</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px] border-r">O</th>
                  {/* Academic */}
                  <th className="px-2 py-2 text-cyan-700 font-medium min-w-[100px]">Acad. Level</th>
                  <th className="px-2 py-2 text-cyan-700 font-medium min-w-[60px]">Score</th>
                  <th className="px-2 py-2 text-cyan-700 font-medium min-w-[70px]">Score Type</th>
                  <th className="px-2 py-2 text-cyan-700 font-medium min-w-[80px] border-r">Country</th>
                  {/* Other */}
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[120px]">Other Req.</th>
                  <th className="px-2 py-2 text-amber-700 font-medium min-w-[120px]">Scholarship</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {coursesLoading ? (
                  <tr><td colSpan={44} className="text-center py-12 text-muted-foreground">Loading courses...</td></tr>
                ) : courses.length === 0 ? (
                  <tr><td colSpan={44} className="text-center py-12 text-muted-foreground">No courses found</td></tr>
                ) : courses.map((c) => (
                  <tr key={c.id} className="hover:bg-blue-50/30 transition-colors">
                    {/* Sticky Course Name */}
                    <td className="sticky left-0 bg-white border-r px-3 py-2 font-medium text-blue-700 hover:underline cursor-pointer min-w-[220px]">
                      <Link href={`/courses/${c.id}`} className="line-clamp-2">{c.name}</Link>
                    </td>
                    <td className="sticky bg-white border-r px-2 py-2 text-gray-600 min-w-[80px]" style={{ left: 220 }}>
                      <span className="line-clamp-1">{txt(c.category)}</span>
                    </td>
                    {/* Details */}
                    <td className="px-2 py-2 text-gray-500"><span className="line-clamp-1">{txt(c.subCategory)}</span></td>
                    <td className="px-2 py-2">
                      {c.courseWebsite ? (
                        <a href={c.courseWebsite} target="_blank" rel="noreferrer" className="text-blue-500 hover:underline">
                          <ExternalLink className="w-3.5 h-3.5" />
                        </a>
                      ) : "—"}
                    </td>
                    <td className="px-2 py-2 text-gray-600">{num(c.duration)}</td>
                    <td className="px-2 py-2 text-gray-500">{txt(c.durationTerm)}</td>
                    <td className="px-2 py-2 text-gray-600">{txt(c.studyMode)}</td>
                    <td className="px-2 py-2 border-r">
                      {c.degreeLevel ? (
                        <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold ${DEGREE_COLORS[c.degreeLevel] ?? "bg-gray-100 text-gray-600"}`}>
                          {c.degreeLevel}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="px-2 py-2 text-gray-500">{txt(c.studyLoad)}</td>
                    <td className="px-2 py-2 text-gray-500 border-r">{txt(c.language)}</td>
                    {/* Intake */}
                    <td className="px-2 py-2 text-blue-600">{txt(c.intakeMonths)}</td>
                    <td className="px-2 py-2 text-blue-500">{num(c.intakeDays)}</td>
                    <td className="px-2 py-2 text-blue-500 border-r">{txt(c.city)}</td>
                    {/* Fee */}
                    <td className="px-2 py-2 text-amber-700 font-medium">{c.internationalFee ? c.internationalFee.toLocaleString() : "—"}</td>
                    <td className="px-2 py-2 text-amber-600">{txt(c.feeTerm)}</td>
                    <td className="px-2 py-2 text-amber-600">{num(c.feeYear)}</td>
                    <td className="px-2 py-2 text-amber-600 border-r">{txt(c.currency)}</td>
                    {/* IELTS */}
                    <td className="px-2 py-2 text-purple-700">{num(c.ieltsListening)}</td>
                    <td className="px-2 py-2 text-purple-700">{num(c.ieltsSpeaking)}</td>
                    <td className="px-2 py-2 text-purple-700">{num(c.ieltsWriting)}</td>
                    <td className="px-2 py-2 text-purple-700">{num(c.ieltsReading)}</td>
                    <td className="px-2 py-2 text-purple-700 font-semibold border-r">{num(c.ieltsOverall)}</td>
                    {/* PTE */}
                    <td className="px-2 py-2 text-orange-600">{num(c.pteListening)}</td>
                    <td className="px-2 py-2 text-orange-600">{num(c.pteSpeaking)}</td>
                    <td className="px-2 py-2 text-orange-600">{num(c.pteWriting)}</td>
                    <td className="px-2 py-2 text-orange-600">{num(c.pteReading)}</td>
                    <td className="px-2 py-2 text-orange-600 font-semibold border-r">{num(c.pteOverall)}</td>
                    {/* TOEFL */}
                    <td className="px-2 py-2 text-rose-600">{num(c.toeflListening)}</td>
                    <td className="px-2 py-2 text-rose-600">{num(c.toeflSpeaking)}</td>
                    <td className="px-2 py-2 text-rose-600">{num(c.toeflWriting)}</td>
                    <td className="px-2 py-2 text-rose-600">{num(c.toeflReading)}</td>
                    <td className="px-2 py-2 text-rose-600 font-semibold border-r">{num(c.toeflOverall)}</td>
                    {/* Other English */}
                    <td className="px-2 py-2 text-pink-600">{txt(c.otherEnglishTestName)}</td>
                    <td className="px-2 py-2 text-pink-500">{num(c.otherEnglishReading)}</td>
                    <td className="px-2 py-2 text-pink-500">{num(c.otherEnglishListening)}</td>
                    <td className="px-2 py-2 text-pink-500">{num(c.otherEnglishSpeaking)}</td>
                    <td className="px-2 py-2 text-pink-500">{num(c.otherEnglishWriting)}</td>
                    <td className="px-2 py-2 text-pink-600 font-semibold border-r">{num(c.otherEnglishOverall)}</td>
                    {/* Academic */}
                    <td className="px-2 py-2 text-cyan-700">{txt(c.academicLevel)}</td>
                    <td className="px-2 py-2 text-cyan-600">{num(c.academicScore)}</td>
                    <td className="px-2 py-2 text-cyan-600">{txt(c.scoreType)}</td>
                    <td className="px-2 py-2 text-cyan-600 border-r">{txt(c.academicCountry)}</td>
                    {/* Other */}
                    <td className="px-2 py-2 text-gray-500 max-w-[140px]">
                      <span className="line-clamp-1">{txt(c.otherRequirement)}</span>
                    </td>
                    <td className="px-2 py-2 text-amber-700 max-w-[140px]">
                      <span className="line-clamp-1">{txt(c.scholarshipDetails)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between pt-1">
              <p className="text-sm text-muted-foreground">
                {(page - 1) * limit + 1}–{Math.min(page * limit, total)} of {total} courses
              </p>
              <div className="flex items-center gap-2">
                <Button variant="outline" size="sm" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}>
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <span className="text-sm">Page {page} / {totalPages}</span>
                <Button variant="outline" size="sm" onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page === totalPages}>
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── ENGLISH PROFICIENCY TAB ── */}
      {tab === "english" && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">{englishCourses.length} course{englishCourses.length !== 1 ? "s" : ""} with English test requirements</p>
          <div className="border rounded-xl overflow-auto" style={{ maxHeight: "70vh" }}>
            <table className="text-xs whitespace-nowrap border-collapse w-full">
              <thead className="bg-gray-50 sticky top-0 z-10">
                <tr className="text-[10px] font-bold text-gray-500 uppercase tracking-wide border-b">
                  <th className="text-left px-4 py-2 border-r" colSpan={2}>Course</th>
                  <th className="text-center px-2 py-2 border-r" colSpan={5} style={{ background: "#fdf4ff", color: "#7e22ce" }}>IELTS</th>
                  <th className="text-center px-2 py-2 border-r" colSpan={5} style={{ background: "#fff7ed", color: "#c2410c" }}>PTE</th>
                  <th className="text-center px-2 py-2 border-r" colSpan={5} style={{ background: "#fef2f2", color: "#be123c" }}>TOEFL</th>
                  <th className="text-center px-2 py-2" colSpan={6} style={{ background: "#fdf2f8", color: "#be185d" }}>Other English Test</th>
                </tr>
                <tr className="border-b bg-gray-50">
                  <th className="text-left px-4 py-2 font-semibold text-gray-700 min-w-[240px]">Course Name</th>
                  <th className="text-left px-2 py-2 font-semibold text-gray-600 min-w-[100px] border-r">Degree Level</th>
                  <th className="px-3 py-2 text-purple-700 font-semibold">L</th>
                  <th className="px-3 py-2 text-purple-700 font-semibold">S</th>
                  <th className="px-3 py-2 text-purple-700 font-semibold">W</th>
                  <th className="px-3 py-2 text-purple-700 font-semibold">R</th>
                  <th className="px-3 py-2 text-purple-700 font-bold border-r">Overall</th>
                  <th className="px-3 py-2 text-orange-600 font-semibold">L</th>
                  <th className="px-3 py-2 text-orange-600 font-semibold">S</th>
                  <th className="px-3 py-2 text-orange-600 font-semibold">W</th>
                  <th className="px-3 py-2 text-orange-600 font-semibold">R</th>
                  <th className="px-3 py-2 text-orange-600 font-bold border-r">Overall</th>
                  <th className="px-3 py-2 text-rose-600 font-semibold">L</th>
                  <th className="px-3 py-2 text-rose-600 font-semibold">S</th>
                  <th className="px-3 py-2 text-rose-600 font-semibold">W</th>
                  <th className="px-3 py-2 text-rose-600 font-semibold">R</th>
                  <th className="px-3 py-2 text-rose-600 font-bold border-r">Overall</th>
                  <th className="px-3 py-2 text-pink-600 font-semibold min-w-[80px]">Test</th>
                  <th className="px-3 py-2 text-pink-500 font-semibold">R</th>
                  <th className="px-3 py-2 text-pink-500 font-semibold">L</th>
                  <th className="px-3 py-2 text-pink-500 font-semibold">S</th>
                  <th className="px-3 py-2 text-pink-500 font-semibold">W</th>
                  <th className="px-3 py-2 text-pink-600 font-bold">Overall</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {englishCourses.length === 0 ? (
                  <tr><td colSpan={23} className="text-center py-12 text-muted-foreground">No English test requirements found</td></tr>
                ) : englishCourses.map((c) => (
                  <tr key={c.id} className="hover:bg-blue-50/30">
                    <td className="px-4 py-2 font-medium text-blue-700">
                      <Link href={`/courses/${c.id}`} className="hover:underline line-clamp-1">{c.name}</Link>
                    </td>
                    <td className="px-2 py-2 border-r">
                      {c.degreeLevel ? (
                        <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold ${DEGREE_COLORS[c.degreeLevel] ?? "bg-gray-100 text-gray-600"}`}>
                          {c.degreeLevel}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="px-3 py-2 text-center text-purple-700">{num(c.ieltsListening)}</td>
                    <td className="px-3 py-2 text-center text-purple-700">{num(c.ieltsSpeaking)}</td>
                    <td className="px-3 py-2 text-center text-purple-700">{num(c.ieltsWriting)}</td>
                    <td className="px-3 py-2 text-center text-purple-700">{num(c.ieltsReading)}</td>
                    <td className="px-3 py-2 text-center text-purple-700 font-bold border-r">{num(c.ieltsOverall)}</td>
                    <td className="px-3 py-2 text-center text-orange-600">{num(c.pteListening)}</td>
                    <td className="px-3 py-2 text-center text-orange-600">{num(c.pteSpeaking)}</td>
                    <td className="px-3 py-2 text-center text-orange-600">{num(c.pteWriting)}</td>
                    <td className="px-3 py-2 text-center text-orange-600">{num(c.pteReading)}</td>
                    <td className="px-3 py-2 text-center text-orange-600 font-bold border-r">{num(c.pteOverall)}</td>
                    <td className="px-3 py-2 text-center text-rose-600">{num(c.toeflListening)}</td>
                    <td className="px-3 py-2 text-center text-rose-600">{num(c.toeflSpeaking)}</td>
                    <td className="px-3 py-2 text-center text-rose-600">{num(c.toeflWriting)}</td>
                    <td className="px-3 py-2 text-center text-rose-600">{num(c.toeflReading)}</td>
                    <td className="px-3 py-2 text-center text-rose-600 font-bold border-r">{num(c.toeflOverall)}</td>
                    <td className="px-3 py-2 text-pink-600">{txt(c.otherEnglishTestName)}</td>
                    <td className="px-3 py-2 text-center text-pink-500">{num(c.otherEnglishReading)}</td>
                    <td className="px-3 py-2 text-center text-pink-500">{num(c.otherEnglishListening)}</td>
                    <td className="px-3 py-2 text-center text-pink-500">{num(c.otherEnglishSpeaking)}</td>
                    <td className="px-3 py-2 text-center text-pink-500">{num(c.otherEnglishWriting)}</td>
                    <td className="px-3 py-2 text-center text-pink-600 font-bold">{num(c.otherEnglishOverall)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── ACADEMIC REQUIREMENTS TAB ── */}
      {tab === "academic" && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">{academicCourses.length} course{academicCourses.length !== 1 ? "s" : ""} with academic requirements</p>
          <div className="border rounded-xl overflow-auto" style={{ maxHeight: "70vh" }}>
            <table className="text-sm border-collapse w-full">
              <thead className="bg-gray-50 sticky top-0 z-10 border-b">
                <tr>
                  <th className="text-left px-4 py-3 font-semibold text-gray-700 min-w-[260px]">Course Name</th>
                  <th className="text-left px-3 py-3 font-semibold text-gray-700 min-w-[110px]">Degree Level</th>
                  <th className="text-left px-3 py-3 font-semibold text-cyan-700 min-w-[140px]">Academic Level</th>
                  <th className="text-left px-3 py-3 font-semibold text-cyan-700 min-w-[80px]">Score</th>
                  <th className="text-left px-3 py-3 font-semibold text-cyan-700 min-w-[90px]">Score Type</th>
                  <th className="text-left px-3 py-3 font-semibold text-cyan-700 min-w-[120px]">Country</th>
                  <th className="text-left px-3 py-3 font-semibold text-gray-600 min-w-[200px]">Other Requirement</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {academicCourses.length === 0 ? (
                  <tr><td colSpan={7} className="text-center py-12 text-muted-foreground">No academic requirements found</td></tr>
                ) : academicCourses.map((c) => (
                  <tr key={c.id} className="hover:bg-blue-50/30">
                    <td className="px-4 py-2.5 font-medium text-blue-700">
                      <Link href={`/courses/${c.id}`} className="hover:underline">{c.name}</Link>
                    </td>
                    <td className="px-3 py-2.5">
                      {c.degreeLevel ? (
                        <span className={`inline-flex px-2 py-0.5 rounded text-xs font-semibold ${DEGREE_COLORS[c.degreeLevel] ?? "bg-gray-100 text-gray-600"}`}>
                          {c.degreeLevel}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-cyan-700">{txt(c.academicLevel)}</td>
                    <td className="px-3 py-2.5 text-cyan-700 font-semibold">{num(c.academicScore)}</td>
                    <td className="px-3 py-2.5 text-cyan-600">{txt(c.scoreType)}</td>
                    <td className="px-3 py-2.5 text-cyan-600">{txt(c.academicCountry)}</td>
                    <td className="px-3 py-2.5 text-gray-500 max-w-[200px]">
                      <span className="line-clamp-2">{txt(c.otherRequirement)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── SCHOLARSHIPS TAB ── */}
      {tab === "scholarships" && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">{scholarshipCourses.length} course{scholarshipCourses.length !== 1 ? "s" : ""} with scholarship information</p>
          {scholarshipCourses.length === 0 ? (
            <div className="border rounded-xl p-12 text-center text-muted-foreground">
              <Award className="w-10 h-10 mx-auto mb-3 opacity-30" />
              <p>No scholarship information available for this university.</p>
            </div>
          ) : (
            <div className="grid gap-3">
              {scholarshipCourses.map((c) => (
                <div key={c.id} className="border rounded-xl p-4 hover:shadow-sm transition-shadow bg-white">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <Link href={`/courses/${c.id}`} className="font-semibold text-blue-700 hover:underline">{c.name}</Link>
                      <div className="flex flex-wrap items-center gap-2 mt-1">
                        {c.degreeLevel && (
                          <span className={`inline-flex px-2 py-0.5 rounded text-xs font-semibold ${DEGREE_COLORS[c.degreeLevel] ?? "bg-gray-100 text-gray-600"}`}>
                            {c.degreeLevel}
                          </span>
                        )}
                        {c.category && <Badge variant="secondary" className="text-xs">{c.category}</Badge>}
                      </div>
                    </div>
                    {c.internationalFee && (
                      <div className="text-right shrink-0">
                        <div className="font-semibold text-amber-700">{c.currency ?? "AUD"} {c.internationalFee.toLocaleString()}</div>
                        <div className="text-xs text-gray-400">per {c.feeTerm ?? "year"}</div>
                      </div>
                    )}
                  </div>
                  <div className="mt-3 rounded-lg bg-amber-50 border border-amber-100 px-3 py-2">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <Award className="w-3.5 h-3.5 text-amber-600" />
                      <span className="text-xs font-semibold text-amber-700">Scholarship</span>
                    </div>
                    <p className="text-sm text-amber-800">{c.scholarshipDetails}</p>
                    {c.scholarshipEligibility && (
                      <p className="text-xs text-amber-600 mt-1">Eligibility: {c.scholarshipEligibility}</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
