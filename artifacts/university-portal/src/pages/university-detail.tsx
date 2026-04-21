import { useState, useEffect, useCallback, useRef } from "react";
import { useRoute, Link } from "wouter";
import { useGetUniversity, getGetUniversityQueryKey, useListCourses } from "@workspace/api-client-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import {
  Building2, MapPin, Globe, Search, ChevronLeft, ChevronRight, X,
  BookOpen, Languages, GraduationCap, Award, ExternalLink,
  Database, CheckCircle2, Clock, Trash2, Pencil, Upload, RefreshCw,
  ChevronsUpDown, Check,
} from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { CATEGORY_NAMES, DEGREE_LEVELS, STUDY_MODES, getSubCategories } from "@/lib/course-constants";

const ALL = "__all__";

const COUNTRIES = [
  "Afghanistan","Albania","Algeria","Andorra","Angola","Antigua and Barbuda","Argentina","Armenia","Australia","Austria",
  "Azerbaijan","Bahamas","Bahrain","Bangladesh","Barbados","Belarus","Belgium","Belize","Benin","Bhutan",
  "Bolivia","Bosnia and Herzegovina","Botswana","Brazil","Brunei","Bulgaria","Burkina Faso","Burundi","Cabo Verde","Cambodia",
  "Cameroon","Canada","Central African Republic","Chad","Chile","China","Colombia","Comoros","Congo","Costa Rica",
  "Croatia","Cuba","Cyprus","Czech Republic","Denmark","Djibouti","Dominica","Dominican Republic","Ecuador","Egypt",
  "El Salvador","Equatorial Guinea","Eritrea","Estonia","Eswatini","Ethiopia","Fiji","Finland","France","Gabon",
  "Gambia","Georgia","Germany","Ghana","Greece","Grenada","Guatemala","Guinea","Guinea-Bissau","Guyana",
  "Haiti","Honduras","Hungary","Iceland","India","Indonesia","Iran","Iraq","Ireland","Israel",
  "Italy","Jamaica","Japan","Jordan","Kazakhstan","Kenya","Kiribati","Kuwait","Kyrgyzstan","Laos",
  "Latvia","Lebanon","Lesotho","Liberia","Libya","Liechtenstein","Lithuania","Luxembourg","Madagascar","Malawi",
  "Malaysia","Maldives","Mali","Malta","Marshall Islands","Mauritania","Mauritius","Mexico","Micronesia","Moldova",
  "Monaco","Mongolia","Montenegro","Morocco","Mozambique","Myanmar","Namibia","Nauru","Nepal","Netherlands",
  "New Zealand","Nicaragua","Niger","Nigeria","North Korea","North Macedonia","Norway","Oman","Pakistan","Palau",
  "Palestine","Panama","Papua New Guinea","Paraguay","Peru","Philippines","Poland","Portugal","Qatar","Romania",
  "Russia","Rwanda","Saint Kitts and Nevis","Saint Lucia","Saint Vincent and the Grenadines","Samoa","San Marino","Sao Tome and Principe","Saudi Arabia","Senegal",
  "Serbia","Seychelles","Sierra Leone","Singapore","Slovakia","Slovenia","Solomon Islands","Somalia","South Africa","South Korea",
  "South Sudan","Spain","Sri Lanka","Sudan","Suriname","Sweden","Switzerland","Syria","Taiwan","Tajikistan",
  "Tanzania","Thailand","Timor-Leste","Togo","Tonga","Trinidad and Tobago","Tunisia","Turkey","Turkmenistan","Tuvalu",
  "Uganda","Ukraine","United Arab Emirates","United Kingdom","United States","Uruguay","Uzbekistan","Vanuatu","Vatican City","Venezuela",
  "Vietnam","Yemen","Zambia","Zimbabwe",
];


type Tab = "courses" | "english" | "academic" | "scholarships" | "rawdata";

const DEGREE_COLORS: Record<string, string> = {
  Bachelor: "bg-blue-100 text-blue-700",
  Master: "bg-purple-100 text-purple-700",
  "PhD": "bg-red-100 text-red-700",
  "Doctor/Doctorate": "bg-red-100 text-red-700",
  "Certificate & Diploma": "bg-green-100 text-green-700",
  "Graduate Certificate & Diploma": "bg-teal-100 text-teal-700",
  "Associate Degree or Equivalent": "bg-orange-100 text-orange-700",
};

function num(v: number | null | undefined) { return v != null ? v : "—"; }
function txt(v: string | null | undefined) { return v || "—"; }

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

type StagedCourse = {
  id: number;
  university_id: number;
  course_name: string;
  status: string;
  degree_level?: string | null;
  category?: string | null;
  sub_category?: string | null;
  course_website?: string | null;
  duration?: number | null;
  duration_term?: string | null;
  study_mode?: string | null;
  study_load?: string | null;
  course_location?: string | null;
  language?: string | null;
  international_fee?: number | null;
  fee_term?: string | null;
  fee_year?: number | null;
  currency?: string | null;
  ielts_overall?: number | null;
  ielts_listening?: number | null;
  ielts_speaking?: number | null;
  ielts_writing?: number | null;
  ielts_reading?: number | null;
  pte_overall?: number | null;
  pte_listening?: number | null;
  pte_speaking?: number | null;
  pte_writing?: number | null;
  pte_reading?: number | null;
  toefl_overall?: number | null;
  toefl_listening?: number | null;
  toefl_speaking?: number | null;
  toefl_writing?: number | null;
  toefl_reading?: number | null;
  cambridge_overall?: number | null;
  duolingo_overall?: number | null;
  intake_months?: string[] | null;
  academic_level?: string | null;
  academic_score?: number | null;
  score_type?: string | null;
  academic_country?: string | null;
  other_requirement?: string | null;
  scholarship?: string | null;
  completeness?: number | null;
  scrape_job_id?: string | null;
  created_at?: string | null;
};

type EditForm = {
  courseName: string;
  degreeLevel: string;
  category: string;
  subCategory: string;
  courseWebsite: string;
  duration: string;
  durationTerm: string;
  studyMode: string;
  studyLoad: string;
  courseLocation: string;
  language: string;
  internationalFee: string;
  feeTerm: string;
  feeYear: string;
  currency: string;
  ieltsOverall: string;
  ieltsListening: string;
  ieltsSpeaking: string;
  ieltsWriting: string;
  ieltsReading: string;
  pteOverall: string;
  pteListening: string;
  pteSpeaking: string;
  pteWriting: string;
  pteReading: string;
  toeflOverall: string;
  toeflListening: string;
  toeflSpeaking: string;
  toeflWriting: string;
  toeflReading: string;
  cambridgeOverall: string;
  duolingoOverall: string;
  intakeMonths: string;
  academicLevel: string;
  academicScore: string;
  scoreType: string;
  academicCountry: string;
  otherRequirement: string;
  scholarship: string;
};

function courseToForm(c: StagedCourse): EditForm {
  return {
    courseName: c.course_name ?? "",
    degreeLevel: c.degree_level ?? "",
    category: c.category ?? "",
    subCategory: c.sub_category ?? "",
    courseWebsite: c.course_website ?? "",
    duration: c.duration != null ? String(c.duration) : "",
    durationTerm: c.duration_term ?? "",
    studyMode: c.study_mode ?? "",
    studyLoad: c.study_load ?? "",
    courseLocation: c.course_location ?? "",
    language: c.language ?? "",
    internationalFee: c.international_fee != null ? String(c.international_fee) : "",
    feeTerm: c.fee_term ?? "",
    feeYear: c.fee_year != null ? String(c.fee_year) : "",
    currency: c.currency ?? "",
    ieltsOverall: c.ielts_overall != null ? String(c.ielts_overall) : "",
    ieltsListening: c.ielts_listening != null ? String(c.ielts_listening) : "",
    ieltsSpeaking: c.ielts_speaking != null ? String(c.ielts_speaking) : "",
    ieltsWriting: c.ielts_writing != null ? String(c.ielts_writing) : "",
    ieltsReading: c.ielts_reading != null ? String(c.ielts_reading) : "",
    pteOverall: c.pte_overall != null ? String(c.pte_overall) : "",
    pteListening: c.pte_listening != null ? String(c.pte_listening) : "",
    pteSpeaking: c.pte_speaking != null ? String(c.pte_speaking) : "",
    pteWriting: c.pte_writing != null ? String(c.pte_writing) : "",
    pteReading: c.pte_reading != null ? String(c.pte_reading) : "",
    toeflOverall: c.toefl_overall != null ? String(c.toefl_overall) : "",
    toeflListening: c.toefl_listening != null ? String(c.toefl_listening) : "",
    toeflSpeaking: c.toefl_speaking != null ? String(c.toefl_speaking) : "",
    toeflWriting: c.toefl_writing != null ? String(c.toefl_writing) : "",
    toeflReading: c.toefl_reading != null ? String(c.toefl_reading) : "",
    cambridgeOverall: c.cambridge_overall != null ? String(c.cambridge_overall) : "",
    duolingoOverall: c.duolingo_overall != null ? String(c.duolingo_overall) : "",
    intakeMonths: Array.isArray(c.intake_months) ? c.intake_months.join(", ") : (c.intake_months ?? ""),
    academicLevel: c.academic_level ?? "",
    academicScore: c.academic_score != null ? String(c.academic_score) : "",
    scoreType: c.score_type ?? "",
    academicCountry: c.academic_country ?? "",
    otherRequirement: c.other_requirement ?? "",
    scholarship: c.scholarship ?? "",
  };
}

function formToPayload(f: EditForm) {
  const n = (v: string) => v.trim() !== "" ? parseFloat(v) : null;
  const s = (v: string) => v.trim() || null;
  const months = f.intakeMonths.trim()
    ? f.intakeMonths.split(",").map((m) => m.trim()).filter(Boolean)
    : null;
  return {
    courseName: f.courseName.trim(),
    degreeLevel: s(f.degreeLevel),
    category: s(f.category),
    subCategory: s(f.subCategory),
    courseWebsite: s(f.courseWebsite),
    duration: n(f.duration),
    durationTerm: s(f.durationTerm),
    studyMode: s(f.studyMode),
    studyLoad: s(f.studyLoad),
    courseLocation: s(f.courseLocation),
    language: s(f.language),
    internationalFee: n(f.internationalFee),
    feeTerm: s(f.feeTerm),
    feeYear: n(f.feeYear),
    currency: s(f.currency),
    ieltsOverall: n(f.ieltsOverall),
    ieltsListening: n(f.ieltsListening),
    ieltsSpeaking: n(f.ieltsSpeaking),
    ieltsWriting: n(f.ieltsWriting),
    ieltsReading: n(f.ieltsReading),
    pteOverall: n(f.pteOverall),
    pteListening: n(f.pteListening),
    pteSpeaking: n(f.pteSpeaking),
    pteWriting: n(f.pteWriting),
    pteReading: n(f.pteReading),
    toeflOverall: n(f.toeflOverall),
    toeflListening: n(f.toeflListening),
    toeflSpeaking: n(f.toeflSpeaking),
    toeflWriting: n(f.toeflWriting),
    toeflReading: n(f.toeflReading),
    cambridgeOverall: n(f.cambridgeOverall),
    duolingoOverall: n(f.duolingoOverall),
    intakeMonths: months,
    academicLevel: s(f.academicLevel),
    academicScore: n(f.academicScore),
    scoreType: s(f.scoreType),
    academicCountry: s(f.academicCountry),
    otherRequirement: s(f.otherRequirement),
    scholarship: s(f.scholarship),
  };
}

function StatusBadge({ status }: { status: string }) {
  if (status === "approved") return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-green-100 text-green-700">
      <CheckCircle2 className="w-3 h-3" /> Approved
    </span>
  );
  if (status === "rejected") return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-red-100 text-red-700">
      <X className="w-3 h-3" /> Rejected
    </span>
  );
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-amber-100 text-amber-700">
      <Clock className="w-3 h-3" /> Pending
    </span>
  );
}

function FldInput({ label, value, onChange, type = "text" }: {
  label: string; value: string; onChange: (v: string) => void; type?: string;
}) {
  return (
    <div className="space-y-1">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-8 text-sm"
      />
    </div>
  );
}

export default function UniversityDetail() {
  const [, params] = useRoute("/universities/:id");
  const id = params?.id ? parseInt(params.id) : 0;
  const { toast } = useToast();

  const [tab, setTab] = useState<Tab>("courses");

  // ── Bulk edit state ──────────────────────────────────────────────
  type BulkMode = "english" | "academic" | "scholarships" | null;
  const [bulkMode, setBulkMode] = useState<BulkMode>(null);
  const [bulkSearch, setBulkSearch] = useState("");
  const [bulkFilter, setBulkFilter] = useState<"all" | "missing" | "hasData">("all");
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [bulkApplying, setBulkApplying] = useState(false);

  // English form state
  const [bEngTestType, setBEngTestType] = useState("IELTS");
  const [bEngL, setBEngL] = useState("");
  const [bEngS, setBEngS] = useState("");
  const [bEngW, setBEngW] = useState("");
  const [bEngR, setBEngR] = useState("");
  const [bEngO, setBEngO] = useState("");
  const [bEngTestName, setBEngTestName] = useState("");

  // Academic form state
  const [bAcadLevel, setBacadLevel] = useState("");
  const [bAcadScore, setBacadScore] = useState("");
  const [bAcadScoreType, setBacadScoreType] = useState("%");
  const [bAcadOutOf, setBacadOutOf] = useState("");
  const [bAcadCountry, setBacadCountry] = useState("");
  const [bAcadCountryOpen, setBacadCountryOpen] = useState(false);

  // Scholarship form state
  const [bSchName, setBSchName] = useState("");
  const [bSchDetails, setBSchDetails] = useState("");
  const [bSchEligibility, setBSchEligibility] = useState("");
  const [bSchAmount, setBSchAmount] = useState("");
  const [bSchCurrency, setBSchCurrency] = useState("AUD");
  const [bSchReplace, setBSchReplace] = useState(false);

  const openBulk = (mode: BulkMode) => {
    setBulkMode(mode);
    setBulkSearch("");
    setBulkFilter("all");
    setSelectedIds(new Set());
    setBacadOutOf("");
  };

  const [search, setSearch] = useState("");
  const [category, setCategory] = useState(ALL);
  const [subCategory, setSubCategory] = useState(ALL);
  const [degreeLevel, setDegreeLevel] = useState(ALL);
  const [studyMode, setStudyMode] = useState(ALL);
  const [page, setPage] = useState(1);
  const limit = 50;

  // Mini scrollbar — pure state-driven, no miniScrollbarRef dependency needed
  const tableScrollRef = useRef<HTMLDivElement>(null);
  const miniScrollbarRef = useRef<HTMLDivElement>(null);
  const TRACK_INNER = 118; // 122px widget width - 4px total padding
  const [thumbLeft, setThumbLeft] = useState(2);
  const [thumbWidth, setThumbWidth] = useState(38);

  const computeThumb = useCallback(() => {
    const viewport = tableScrollRef.current;
    if (!viewport) return;
    const visible = viewport.clientWidth;
    const total = viewport.scrollWidth;
    if (total <= visible) {
      setThumbWidth(TRACK_INNER);
      setThumbLeft(2);
      return;
    }
    const tw = Math.max(20, Math.floor(TRACK_INNER * (visible / total)));
    const maxLeft = TRACK_INNER - tw;
    const ratio = viewport.scrollLeft / (total - visible);
    setThumbWidth(tw);
    setThumbLeft(2 + Math.round(maxLeft * ratio));
  }, []);

  useEffect(() => {
    if (tab !== "courses") return;
    // Poll until the table ref is set (data may load async)
    let cleanupCalled = false;
    let removeListeners: (() => void) | null = null;
    const attach = () => {
      const viewport = tableScrollRef.current;
      if (!viewport) return false;
      const onScroll = () => computeThumb();
      viewport.addEventListener("scroll", onScroll, { passive: true });
      window.addEventListener("resize", computeThumb);
      removeListeners = () => {
        viewport.removeEventListener("scroll", onScroll);
        window.removeEventListener("resize", computeThumb);
      };
      computeThumb();
      return true;
    };
    // Retry attaching until the DOM ref exists (covers async data render)
    if (!attach()) {
      const id = setInterval(() => {
        if (cleanupCalled || attach()) clearInterval(id);
      }, 50);
    }
    return () => {
      cleanupCalled = true;
      removeListeners?.();
    };
  }, [tab, computeThumb]);

  // Re-sync thumb whenever course data changes (new page, filter, etc.)
  useEffect(() => {
    const t = setTimeout(computeThumb, 80);
    return () => clearTimeout(t);
  }, [page, search, category, subCategory, degreeLevel, studyMode, computeThumb]);

  const handleThumbMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    const viewport = tableScrollRef.current;
    if (!viewport) return;
    const dragStartX = e.clientX;
    const startScrollLeft = viewport.scrollLeft;
    const maxScroll = viewport.scrollWidth - viewport.clientWidth;
    const tw = Math.max(20, Math.floor(TRACK_INNER * (viewport.clientWidth / viewport.scrollWidth)));
    const maxLeft = TRACK_INNER - tw;
    const onMouseMove = (ev: MouseEvent) => {
      const dx = ev.clientX - dragStartX;
      const scrollRatio = maxLeft > 0 ? dx / maxLeft : 0;
      viewport.scrollLeft = Math.max(0, Math.min(maxScroll, startScrollLeft + scrollRatio * maxScroll));
    };
    const onMouseUp = () => {
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
    };
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  };

  const handleTrackClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const viewport = tableScrollRef.current;
    const track = miniScrollbarRef.current;
    if (!viewport || !track) return;
    const rect = track.getBoundingClientRect();
    const clickX = e.clientX - rect.left - 2; // relative to inner track
    const tw = thumbWidth;
    const maxLeft = TRACK_INNER - tw;
    let targetLeft = clickX - tw / 2;
    targetLeft = Math.max(0, Math.min(maxLeft, targetLeft));
    const ratio = maxLeft > 0 ? targetLeft / maxLeft : 0;
    viewport.scrollLeft = ratio * (viewport.scrollWidth - viewport.clientWidth);
  };

  const subCategories = category !== ALL ? getSubCategories(category) : [];

  const { data: uni, isLoading: uniLoading } = useGetUniversity(id, {
    query: { enabled: !!id, queryKey: getGetUniversityQueryKey(id) },
  });

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

  const { data: allCoursesData } = useListCourses(
    { universityId: id, limit: 500 },
    { query: { enabled: !!id && (tab === "english" || tab === "academic" || tab === "scholarships") } },
  );

  const courses = coursesData?.data ?? [];
  const total = coursesData?.total ?? 0;
  const totalPages = Math.ceil(total / limit);
  const hasFilters = category !== ALL || subCategory !== ALL || degreeLevel !== ALL || studyMode !== ALL || search;

  const allCourses = allCoursesData?.data ?? [];
  const englishCourses = allCourses.filter(
    (c) => c.ieltsOverall || c.pteOverall || c.toeflOverall || c.ieltsListening || c.pteListening || c.toeflListening,
  );
  const academicCourses = allCourses.filter((c) => c.academicLevel || c.academicScore || c.academicCountry);
  const scholarshipCourses = allCourses.filter((c) => c.scholarshipDetails);

  function clearFilters() {
    setSearch(""); setCategory(ALL); setSubCategory(ALL); setDegreeLevel(ALL); setStudyMode(ALL); setPage(1);
  }
  function handleCategoryChange(val: string) { setCategory(val); setSubCategory(ALL); setPage(1); }

  // ── Raw Data tab state ───────────────────────────────────────────────────
  const [rawStatus, setRawStatus] = useState<"all" | "pending" | "approved">("pending");
  const [rawSearch, setRawSearch] = useState("");
  const [rawData, setRawData] = useState<StagedCourse[]>([]);
  const [rawLoading, setRawLoading] = useState(false);
  const [editingCourse, setEditingCourse] = useState<StagedCourse | null>(null);
  const [editForm, setEditForm] = useState<EditForm | null>(null);
  const [saving, setSaving] = useState(false);
  const [approvingId, setApprovingId] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [importingAll, setImportingAll] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  const [confirmImportAllOpen, setConfirmImportAllOpen] = useState(false);

  const fetchRawData = useCallback(async () => {
    if (!id) return;
    setRawLoading(true);
    try {
      const res = await fetch(`${BASE}/api/scrape/staged?universityId=${id}&status=${rawStatus}`);
      const data = await res.json();
      setRawData(Array.isArray(data) ? data : []);
    } catch {
      toast({ title: "Error", description: "Failed to load raw data", variant: "destructive" });
    } finally {
      setRawLoading(false);
    }
  }, [id, rawStatus, toast]);

  useEffect(() => {
    if (tab === "rawdata") fetchRawData();
  }, [tab, fetchRawData]);

  const filteredRaw = rawData.filter((c) =>
    !rawSearch || c.course_name.toLowerCase().includes(rawSearch.toLowerCase()),
  );

  const pendingCount = rawData.filter((c) => c.status === "pending").length;

  async function handleApprove(courseId: number) {
    setApprovingId(courseId);
    try {
      const res = await fetch(`${BASE}/api/scrape/staged/${courseId}/approve`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Approve failed");
      toast({ title: "Approved", description: "Course imported to production." });
      await fetchRawData();
    } catch (e) {
      toast({ title: "Error", description: (e as Error).message, variant: "destructive" });
    } finally {
      setApprovingId(null);
    }
  }

  function handleDelete(courseId: number) {
    setConfirmDeleteId(courseId);
  }

  async function performDelete(courseId: number) {
    setConfirmDeleteId(null);
    setDeletingId(courseId);
    try {
      const res = await fetch(`${BASE}/api/scrape/staged/${courseId}`, { method: "DELETE" });
      if (!res.ok) throw new Error("Delete failed");
      toast({ title: "Deleted", description: "Staged course removed." });
      setRawData((prev) => prev.filter((c) => c.id !== courseId));
    } catch (e) {
      toast({ title: "Error", description: (e as Error).message, variant: "destructive" });
    } finally {
      setDeletingId(null);
    }
  }

  function handleImportAll() {
    const pending = rawData.filter((c) => c.status === "pending");
    if (!pending.length) return;
    setConfirmImportAllOpen(true);
  }

  async function performImportAll() {
    setConfirmImportAllOpen(false);
    const pending = rawData.filter((c) => c.status === "pending");
    setImportingAll(true);
    let succeeded = 0;
    let failed = 0;
    const errors: string[] = [];
    for (const c of pending) {
      try {
        const res = await fetch(`${BASE}/api/scrape/staged/${c.id}/approve`, { method: "POST" });
        if (res.ok) {
          succeeded++;
        } else {
          const data = await res.json().catch(() => ({}));
          errors.push(data.error || "Unknown error");
          failed++;
        }
      } catch (e) {
        errors.push((e as Error).message);
        failed++;
      }
    }
    toast({
      title: "Import complete",
      description: failed
        ? `${succeeded} imported, ${failed} failed${errors.length ? `: ${errors[0]}` : ""}.`
        : `${succeeded} course${succeeded !== 1 ? "s" : ""} imported successfully.`,
      variant: failed > 0 && succeeded === 0 ? "destructive" : "default",
    });
    await fetchRawData();
    setImportingAll(false);
  }

  function openEdit(c: StagedCourse) {
    setEditingCourse(c);
    setEditForm(courseToForm(c));
  }

  function setField<K extends keyof EditForm>(key: K, val: string) {
    setEditForm((f) => f ? { ...f, [key]: val } : f);
  }

  async function handleSaveEdit() {
    if (!editingCourse || !editForm) return;
    setSaving(true);
    try {
      const payload = formToPayload(editForm);
      const res = await fetch(`${BASE}/api/scrape/staged/${editingCourse.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Save failed");
      toast({ title: "Saved", description: "Course updated." });
      setEditingCourse(null);
      setEditForm(null);
      await fetchRawData();
    } catch (e) {
      toast({ title: "Error", description: (e as Error).message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  }

  const TABS: { key: Tab; label: string; icon: React.ReactNode; count?: number }[] = [
    { key: "courses", label: "Courses", icon: <BookOpen className="w-4 h-4" />, count: uni ? total : undefined },
    { key: "english", label: "English Proficiency", icon: <Languages className="w-4 h-4" /> },
    { key: "academic", label: "Academic Requirements", icon: <GraduationCap className="w-4 h-4" /> },
    { key: "scholarships", label: "Scholarships", icon: <Award className="w-4 h-4" /> },
    { key: "rawdata", label: "Raw Data", icon: <Database className="w-4 h-4" /> },
  ];

  // ── Bulk apply handler ──────────────────────────────────────────
  const applyBulk = async () => {
    if (selectedIds.size === 0 || !bulkMode) return;
    const courseIds = Array.from(selectedIds);
    setBulkApplying(true);
    try {
      let endpoint = "";
      let body: Record<string, unknown> = { courseIds };
      if (bulkMode === "english") {
        endpoint = `${BASE}/api/universities/${id}/bulk-english`;
        body = { courseIds, testType: bEngTestType, listening: bEngL ? Number(bEngL) : null, speaking: bEngS ? Number(bEngS) : null, writing: bEngW ? Number(bEngW) : null, reading: bEngR ? Number(bEngR) : null, overall: bEngO ? Number(bEngO) : null, testName: bEngTestName || null };
      } else if (bulkMode === "academic") {
        endpoint = `${BASE}/api/universities/${id}/bulk-academic`;
        const combinedScoreType = bAcadScoreType ? (bAcadOutOf ? `${bAcadScoreType}/${bAcadOutOf}` : bAcadScoreType) : null;
        body = { courseIds, academicLevel: bAcadLevel || null, academicScore: bAcadScore ? Number(bAcadScore) : null, scoreType: combinedScoreType, academicCountry: bAcadCountry || null };
      } else if (bulkMode === "scholarships") {
        endpoint = `${BASE}/api/universities/${id}/bulk-scholarships`;
        body = { courseIds, name: bSchName, details: bSchDetails || null, eligibilityCriteria: bSchEligibility || null, amount: bSchAmount ? Number(bSchAmount) : null, currency: bSchCurrency || null, replaceExisting: bSchReplace };
      }
      const res = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json() as { updated: number };
      toast({ title: "Bulk update applied", description: `Updated ${data.updated} course${data.updated !== 1 ? "s" : ""}` });
      setBulkMode(null);
      // Refresh course data
      setTimeout(() => window.location.reload(), 500);
    } catch (err) {
      toast({ title: "Error", description: String(err), variant: "destructive" });
    } finally {
      setBulkApplying(false);
    }
  };

  if (uniLoading) return <div className="py-16 text-center text-muted-foreground">Loading...</div>;
  if (!uni) return <div className="py-16 text-center text-muted-foreground">University not found</div>;

  return (
    <>
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
      <div className="border-b flex gap-0 overflow-x-auto">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex items-center gap-2 px-5 py-3 text-sm font-medium border-b-2 transition-colors -mb-px whitespace-nowrap ${
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

          <div ref={tableScrollRef} className="border rounded-xl overflow-auto" style={{ maxHeight: "70vh" }}>
            <table className="text-xs whitespace-nowrap border-collapse" style={{ minWidth: 3000 }}>
              <thead className="bg-gray-50 sticky top-0 z-20">
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
                <tr className="border-b bg-gray-50">
                  <th className="sticky left-0 z-30 bg-gray-50 border-r px-3 py-2 text-left font-semibold text-gray-700 min-w-[220px]">Course Name</th>
                  <th className="sticky bg-gray-50 border-r px-2 py-2 text-left font-semibold text-gray-700 min-w-[80px]" style={{ left: 220, zIndex: 29 }}>Category</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[100px]">Sub Category</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px]">Website</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[70px]">Duration</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px]">Term</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[80px]">Study Mode</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[120px] border-r">Degree Level</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px]">Study Load</th>
                  <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px] border-r">Language</th>
                  <th className="px-2 py-2 text-blue-700 font-medium min-w-[90px]">Month</th>
                  <th className="px-2 py-2 text-blue-700 font-medium min-w-[50px]">Day</th>
                  <th className="px-2 py-2 text-blue-700 font-medium min-w-[40px] border-r">City</th>
                  <th className="px-2 py-2 text-amber-700 font-medium min-w-[70px]">Int'l Fee</th>
                  <th className="px-2 py-2 text-amber-700 font-medium min-w-[60px]">Fee Term</th>
                  <th className="px-2 py-2 text-amber-700 font-medium min-w-[50px]">Year</th>
                  <th className="px-2 py-2 text-amber-700 font-medium min-w-[55px] border-r">Currency</th>
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px]">L</th>
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px]">S</th>
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px]">W</th>
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px]">R</th>
                  <th className="px-2 py-2 text-purple-700 font-medium min-w-[40px] border-r">O</th>
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px]">L</th>
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px]">S</th>
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px]">W</th>
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px]">R</th>
                  <th className="px-2 py-2 text-orange-700 font-medium min-w-[40px] border-r">O</th>
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px]">L</th>
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px]">S</th>
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px]">W</th>
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px]">R</th>
                  <th className="px-2 py-2 text-rose-700 font-medium min-w-[40px] border-r">O</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[80px]">Other Test</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px]">R</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px]">L</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px]">S</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px]">W</th>
                  <th className="px-2 py-2 text-pink-700 font-medium min-w-[40px] border-r">O</th>
                  <th className="px-2 py-2 text-cyan-700 font-medium min-w-[100px]">Acad. Level</th>
                  <th className="px-2 py-2 text-cyan-700 font-medium min-w-[60px]">Score</th>
                  <th className="px-2 py-2 text-cyan-700 font-medium min-w-[70px]">Score Type</th>
                  <th className="px-2 py-2 text-cyan-700 font-medium min-w-[80px] border-r">Country</th>
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
                    <td className="sticky left-0 bg-white border-r px-3 py-2 font-medium text-blue-700 hover:underline cursor-pointer min-w-[220px]">
                      <span className="line-clamp-2">{c.name}</span>
                    </td>
                    <td className="sticky bg-white border-r px-2 py-2 text-gray-600 min-w-[80px]" style={{ left: 220 }}>
                      <span className="line-clamp-1">{txt(c.category)}</span>
                    </td>
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
                    <td className="px-2 py-2 text-blue-600">{txt(c.intakeMonths)}</td>
                    <td className="px-2 py-2 text-blue-500">{num(c.intakeDays)}</td>
                    <td className="px-2 py-2 text-blue-500 border-r">{txt(c.city)}</td>
                    <td className="px-2 py-2 text-amber-700 font-medium">{c.internationalFee ? c.internationalFee.toLocaleString() : "—"}</td>
                    <td className="px-2 py-2 text-amber-600">{txt(c.feeTerm)}</td>
                    <td className="px-2 py-2 text-amber-600">{num(c.feeYear)}</td>
                    <td className="px-2 py-2 text-amber-600 border-r">{txt(c.currency)}</td>
                    <td className="px-2 py-2 text-purple-700">{num(c.ieltsListening)}</td>
                    <td className="px-2 py-2 text-purple-700">{num(c.ieltsSpeaking)}</td>
                    <td className="px-2 py-2 text-purple-700">{num(c.ieltsWriting)}</td>
                    <td className="px-2 py-2 text-purple-700">{num(c.ieltsReading)}</td>
                    <td className="px-2 py-2 text-purple-700 font-semibold border-r">{num(c.ieltsOverall)}</td>
                    <td className="px-2 py-2 text-orange-600">{num(c.pteListening)}</td>
                    <td className="px-2 py-2 text-orange-600">{num(c.pteSpeaking)}</td>
                    <td className="px-2 py-2 text-orange-600">{num(c.pteWriting)}</td>
                    <td className="px-2 py-2 text-orange-600">{num(c.pteReading)}</td>
                    <td className="px-2 py-2 text-orange-600 font-semibold border-r">{num(c.pteOverall)}</td>
                    <td className="px-2 py-2 text-rose-600">{num(c.toeflListening)}</td>
                    <td className="px-2 py-2 text-rose-600">{num(c.toeflSpeaking)}</td>
                    <td className="px-2 py-2 text-rose-600">{num(c.toeflWriting)}</td>
                    <td className="px-2 py-2 text-rose-600">{num(c.toeflReading)}</td>
                    <td className="px-2 py-2 text-rose-600 font-semibold border-r">{num(c.toeflOverall)}</td>
                    <td className="px-2 py-2 text-pink-600">{txt(c.otherEnglishTestName)}</td>
                    <td className="px-2 py-2 text-pink-500">{num(c.otherEnglishReading)}</td>
                    <td className="px-2 py-2 text-pink-500">{num(c.otherEnglishListening)}</td>
                    <td className="px-2 py-2 text-pink-500">{num(c.otherEnglishSpeaking)}</td>
                    <td className="px-2 py-2 text-pink-500">{num(c.otherEnglishWriting)}</td>
                    <td className="px-2 py-2 text-pink-600 font-semibold border-r">{num(c.otherEnglishOverall)}</td>
                    <td className="px-2 py-2 text-cyan-700">{txt(c.academicLevel)}</td>
                    <td className="px-2 py-2 text-cyan-600">{num(c.academicScore)}</td>
                    <td className="px-2 py-2 text-cyan-600">{txt(c.scoreType)}</td>
                    <td className="px-2 py-2 text-cyan-600 border-r">{txt(c.academicCountry)}</td>
                    <td className="px-2 py-2 text-gray-500 max-w-[140px]"><span className="line-clamp-1">{txt(c.otherRequirement)}</span></td>
                    <td className="px-2 py-2 text-amber-700 max-w-[140px]"><span className="line-clamp-1">{txt(c.scholarshipDetails)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Mini Trello-style horizontal scroll indicator */}
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
            <div
              ref={miniScrollbarRef}
              onClick={handleTrackClick}
              style={{
                position: "relative",
                width: 122,
                height: 42,
                borderRadius: 8,
                background: "#f7f7f8",
                border: "1px solid #e3e5e8",
                boxShadow: "inset 0 1px 2px rgba(0,0,0,0.06)",
                overflow: "hidden",
                cursor: "pointer",
                flexShrink: 0,
              }}
              aria-label="Drag to scroll table horizontally"
            >
              {/* Track background stripes */}
              <div style={{ position: "absolute", inset: 0, display: "flex", gap: 4, padding: 6, pointerEvents: "none" }}>
                {[0,1,2,3,4,5].map((i) => (
                  <div key={i} style={{ flex: 1, background: "#e7e7e8", borderRadius: 3, opacity: 0.9 }} />
                ))}
              </div>
              {/* Draggable thumb */}
              <div
                onMouseDown={handleThumbMouseDown}
                style={{
                  position: "absolute",
                  top: 2,
                  left: thumbLeft,
                  height: "calc(100% - 4px)",
                  width: thumbWidth,
                  borderRadius: 6,
                  background: "rgba(255,255,255,0.3)",
                  border: "2px solid #4e73b8",
                  boxShadow: "0 2px 6px rgba(0,0,0,0.18), inset 0 0 0 1px rgba(255,255,255,0.6)",
                  overflow: "hidden",
                  cursor: "grab",
                }}
              >
                <div style={{ display: "flex", gap: 4, height: "100%", padding: 4 }}>
                  {[0,1,2,3].map((i) => (
                    <div key={i} style={{ flex: 1, background: "#dcdcdc", borderRadius: 2 }} />
                  ))}
                </div>
              </div>
            </div>
          </div>

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
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">{englishCourses.length} course{englishCourses.length !== 1 ? "s" : ""} with English test requirements</p>
            <Button size="sm" variant="outline" onClick={() => openBulk("english")} className="gap-1.5 text-purple-700 border-purple-200 hover:bg-purple-50">
              <Pencil className="w-3.5 h-3.5" /> Bulk Edit English
            </Button>
          </div>
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
                      <span className="line-clamp-1">{c.name}</span>
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
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">{academicCourses.length} course{academicCourses.length !== 1 ? "s" : ""} with academic requirements</p>
            <Button size="sm" variant="outline" onClick={() => openBulk("academic")} className="gap-1.5 text-cyan-700 border-cyan-200 hover:bg-cyan-50">
              <Pencil className="w-3.5 h-3.5" /> Bulk Edit Academic
            </Button>
          </div>
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
                      <span className="hover:underline">{c.name}</span>
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
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">{scholarshipCourses.length} course{scholarshipCourses.length !== 1 ? "s" : ""} with scholarship information</p>
            <Button size="sm" variant="outline" onClick={() => openBulk("scholarships")} className="gap-1.5 text-amber-700 border-amber-200 hover:bg-amber-50">
              <Pencil className="w-3.5 h-3.5" /> Bulk Add Scholarship
            </Button>
          </div>
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
                      <span className="font-semibold text-blue-700">{c.name}</span>
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

      {/* ── RAW DATA TAB ── */}
      {tab === "rawdata" && (
        <div className="space-y-4">
          {/* Toolbar */}
          <div className="flex flex-wrap gap-2 items-center">
            {/* Status filter */}
            <div className="flex rounded-lg border overflow-hidden text-sm font-medium">
              {(["all", "pending", "approved"] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => setRawStatus(s)}
                  className={`px-3 py-1.5 capitalize transition-colors ${
                    rawStatus === s
                      ? "bg-primary text-primary-foreground"
                      : "bg-white text-muted-foreground hover:bg-muted"
                  }`}
                >
                  {s}
                  {s === "pending" && pendingCount > 0 && (
                    <span className="ml-1.5 bg-amber-500 text-white text-[10px] px-1.5 py-0.5 rounded-full">{pendingCount}</span>
                  )}
                </button>
              ))}
            </div>

            {/* Search */}
            <div className="flex items-center gap-1.5 border rounded-md px-2 h-9 flex-1 min-w-[180px] max-w-xs bg-white">
              <Search className="h-4 w-4 text-muted-foreground shrink-0" />
              <Input
                placeholder="Search courses..."
                value={rawSearch}
                onChange={(e) => setRawSearch(e.target.value)}
                className="border-0 focus-visible:ring-0 px-0 h-8 bg-transparent"
              />
            </div>

            <span className="text-sm text-muted-foreground">
              {filteredRaw.length} course{filteredRaw.length !== 1 ? "s" : ""}
            </span>

            <div className="ml-auto flex gap-2">
              <Button variant="outline" size="sm" onClick={fetchRawData} disabled={rawLoading}>
                <RefreshCw className={`h-4 w-4 mr-1.5 ${rawLoading ? "animate-spin" : ""}`} />
                Refresh
              </Button>
              {pendingCount > 0 && (
                <Button
                  size="sm"
                  onClick={handleImportAll}
                  disabled={importingAll}
                  className="bg-green-600 hover:bg-green-700 text-white"
                >
                  <Upload className="h-4 w-4 mr-1.5" />
                  {importingAll ? "Importing…" : `Import All (${pendingCount})`}
                </Button>
              )}
            </div>
          </div>

          {/* Table */}
          {rawLoading ? (
            <div className="border rounded-xl py-16 text-center text-muted-foreground">Loading raw data…</div>
          ) : filteredRaw.length === 0 ? (
            <div className="border rounded-xl py-16 text-center text-muted-foreground">
              <Database className="w-10 h-10 mx-auto mb-3 opacity-20" />
              <p>No scraped courses found{rawStatus !== "all" ? ` with status "${rawStatus}"` : ""}.</p>
              <p className="text-xs mt-1">Run a scrape job for this university to populate raw data.</p>
            </div>
          ) : (
            <div className="border rounded-xl overflow-auto" style={{ maxHeight: "70vh" }}>
              <table className="text-xs whitespace-nowrap border-collapse" style={{ minWidth: 2400 }}>
                <thead className="bg-gray-50 sticky top-0 z-20">
                  <tr className="text-[10px] font-bold text-gray-500 uppercase tracking-wide border-b">
                    <th className="sticky left-0 z-30 bg-gray-50 border-r px-3 py-2 text-left min-w-[32px]">#</th>
                    <th className="sticky bg-gray-50 border-r px-3 py-2 text-left min-w-[220px]" style={{ left: 32 }}>Course Name</th>
                    <th className="px-2 py-2 border-r text-center min-w-[80px]">Status</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[110px]">Degree Level</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[100px]">Category</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[70px]">Duration</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[60px]">Term</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[80px] border-r">Mode</th>
                    <th className="px-2 py-2 text-amber-700 font-medium min-w-[80px]">Int'l Fee</th>
                    <th className="px-2 py-2 text-amber-700 font-medium min-w-[55px]">Term</th>
                    <th className="px-2 py-2 text-amber-700 font-medium min-w-[45px]">Year</th>
                    <th className="px-2 py-2 text-amber-700 font-medium min-w-[50px] border-r">Curr.</th>
                    <th className="px-2 py-2 text-blue-700 font-medium min-w-[90px] border-r">Intakes</th>
                    <th className="px-2 py-2 text-purple-700 font-medium min-w-[30px]">IL</th>
                    <th className="px-2 py-2 text-purple-700 font-medium min-w-[30px]">IS</th>
                    <th className="px-2 py-2 text-purple-700 font-medium min-w-[30px]">IW</th>
                    <th className="px-2 py-2 text-purple-700 font-medium min-w-[30px]">IR</th>
                    <th className="px-2 py-2 text-purple-700 font-semibold min-w-[30px] border-r">IO</th>
                    <th className="px-2 py-2 text-orange-600 font-medium min-w-[30px]">PL</th>
                    <th className="px-2 py-2 text-orange-600 font-medium min-w-[30px]">PS</th>
                    <th className="px-2 py-2 text-orange-600 font-medium min-w-[30px]">PW</th>
                    <th className="px-2 py-2 text-orange-600 font-medium min-w-[30px]">PR</th>
                    <th className="px-2 py-2 text-orange-600 font-semibold min-w-[30px] border-r">PO</th>
                    <th className="px-2 py-2 text-rose-600 font-medium min-w-[30px]">TL</th>
                    <th className="px-2 py-2 text-rose-600 font-medium min-w-[30px]">TS</th>
                    <th className="px-2 py-2 text-rose-600 font-medium min-w-[30px]">TW</th>
                    <th className="px-2 py-2 text-rose-600 font-medium min-w-[30px]">TR</th>
                    <th className="px-2 py-2 text-rose-600 font-semibold min-w-[30px] border-r">TO</th>
                    <th className="px-2 py-2 text-pink-600 font-medium min-w-[30px] border-r">CAE</th>
                    <th className="px-2 py-2 text-cyan-700 font-medium min-w-[100px]">Acad. Level</th>
                    <th className="px-2 py-2 text-cyan-700 font-medium min-w-[55px] border-r">Score</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[50px]">%</th>
                    <th className="px-2 py-2 text-gray-600 font-medium min-w-[100px]">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {filteredRaw.map((c, idx) => (
                    <tr
                      key={c.id}
                      className={`transition-colors ${
                        c.status === "approved" ? "bg-green-50/30 hover:bg-green-50/50" :
                        c.status === "rejected" ? "bg-red-50/30 hover:bg-red-50/50" :
                        "hover:bg-blue-50/20"
                      }`}
                    >
                      <td className="sticky left-0 bg-inherit border-r px-3 py-2 text-muted-foreground font-mono">{idx + 1}</td>
                      <td className="sticky bg-inherit border-r px-3 py-2 font-medium text-gray-800 min-w-[220px]" style={{ left: 32 }}>
                        <div className="flex items-center gap-1.5">
                          <span className="line-clamp-1 max-w-[200px]">{c.course_name}</span>
                          {c.course_website && (
                            <a href={c.course_website} target="_blank" rel="noreferrer" className="text-blue-400 shrink-0">
                              <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                      </td>
                      <td className="px-2 py-2 border-r text-center"><StatusBadge status={c.status} /></td>
                      <td className="px-2 py-2">
                        {c.degree_level ? (
                          <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold ${DEGREE_COLORS[c.degree_level] ?? "bg-gray-100 text-gray-600"}`}>
                            {c.degree_level}
                          </span>
                        ) : "—"}
                      </td>
                      <td className="px-2 py-2 text-gray-500">{txt(c.category)}</td>
                      <td className="px-2 py-2 text-gray-600">{num(c.duration)}</td>
                      <td className="px-2 py-2 text-gray-500">{txt(c.duration_term)}</td>
                      <td className="px-2 py-2 text-gray-500 border-r">{txt(c.study_mode)}</td>
                      <td className="px-2 py-2 text-amber-700 font-medium">{c.international_fee ? c.international_fee.toLocaleString() : "—"}</td>
                      <td className="px-2 py-2 text-amber-600">{txt(c.fee_term)}</td>
                      <td className="px-2 py-2 text-amber-600">{c.fee_year ?? "—"}</td>
                      <td className="px-2 py-2 text-amber-600 border-r">{txt(c.currency)}</td>
                      <td className="px-2 py-2 text-blue-600 border-r">{Array.isArray(c.intake_months) ? c.intake_months.join(", ") : txt(c.intake_months as string | null)}</td>
                      <td className="px-2 py-2 text-purple-600">{num(c.ielts_listening)}</td>
                      <td className="px-2 py-2 text-purple-600">{num(c.ielts_speaking)}</td>
                      <td className="px-2 py-2 text-purple-600">{num(c.ielts_writing)}</td>
                      <td className="px-2 py-2 text-purple-600">{num(c.ielts_reading)}</td>
                      <td className="px-2 py-2 text-purple-700 font-semibold border-r">{num(c.ielts_overall)}</td>
                      <td className="px-2 py-2 text-orange-500">{num(c.pte_listening)}</td>
                      <td className="px-2 py-2 text-orange-500">{num(c.pte_speaking)}</td>
                      <td className="px-2 py-2 text-orange-500">{num(c.pte_writing)}</td>
                      <td className="px-2 py-2 text-orange-500">{num(c.pte_reading)}</td>
                      <td className="px-2 py-2 text-orange-600 font-semibold border-r">{num(c.pte_overall)}</td>
                      <td className="px-2 py-2 text-rose-500">{num(c.toefl_listening)}</td>
                      <td className="px-2 py-2 text-rose-500">{num(c.toefl_speaking)}</td>
                      <td className="px-2 py-2 text-rose-500">{num(c.toefl_writing)}</td>
                      <td className="px-2 py-2 text-rose-500">{num(c.toefl_reading)}</td>
                      <td className="px-2 py-2 text-rose-600 font-semibold border-r">{num(c.toefl_overall)}</td>
                      <td className="px-2 py-2 text-pink-600 font-semibold border-r">{num(c.cambridge_overall)}</td>
                      <td className="px-2 py-2 text-cyan-700">{txt(c.academic_level)}</td>
                      <td className="px-2 py-2 text-cyan-600 font-semibold border-r">{num(c.academic_score)}</td>
                      <td className="px-2 py-2 text-muted-foreground">
                        {c.completeness != null ? (
                          <span className={`font-semibold ${c.completeness >= 80 ? "text-green-600" : c.completeness >= 50 ? "text-amber-600" : "text-red-500"}`}>
                            {c.completeness}%
                          </span>
                        ) : "—"}
                      </td>
                      <td className="px-2 py-2">
                        <div className="flex items-center gap-1">
                          <button
                            onClick={() => openEdit(c)}
                            title="Edit"
                            className="p-1 rounded hover:bg-blue-100 text-blue-600"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                          {c.status === "pending" && (
                            <button
                              onClick={() => handleApprove(c.id)}
                              disabled={approvingId === c.id}
                              title="Approve & Import"
                              className="p-1 rounded hover:bg-green-100 text-green-600 disabled:opacity-40"
                            >
                              <CheckCircle2 className="w-3.5 h-3.5" />
                            </button>
                          )}
                          <button
                            onClick={() => handleDelete(c.id)}
                            disabled={deletingId === c.id}
                            title="Delete"
                            className="p-1 rounded hover:bg-red-100 text-red-500 disabled:opacity-40"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── Confirm Delete Dialog ── */}
      {confirmDeleteId !== null && (
        <Dialog open onOpenChange={() => setConfirmDeleteId(null)}>
          <DialogContent className="max-w-sm">
            <DialogHeader>
              <DialogTitle>Delete staged course?</DialogTitle>
            </DialogHeader>
            <p className="text-sm text-muted-foreground py-2">
              This will permanently remove the staged course from the review queue. This cannot be undone.
            </p>
            <DialogFooter className="gap-2 sm:gap-0">
              <Button variant="outline" onClick={() => setConfirmDeleteId(null)}>Cancel</Button>
              <Button variant="destructive" onClick={() => performDelete(confirmDeleteId)}>Delete</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {/* ── Confirm Import All Dialog ── */}
      <Dialog open={confirmImportAllOpen} onOpenChange={setConfirmImportAllOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Import all pending courses?</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground py-2">
            This will import all <strong>{pendingCount}</strong> pending course{pendingCount !== 1 ? "s" : ""} to production. Each course's current staged data will be published.
          </p>
          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" onClick={() => setConfirmImportAllOpen(false)}>Cancel</Button>
            <Button onClick={performImportAll}>Import All</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Edit Course Dialog ── */}
      {editingCourse && editForm && (
        <Dialog open onOpenChange={() => { setEditingCourse(null); setEditForm(null); }}>
          <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Edit Course — {editingCourse.course_name}</DialogTitle>
            </DialogHeader>

            <div className="space-y-5 py-2">
              {/* Basic */}
              <div>
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">Basic Info</p>
                <div className="grid grid-cols-2 gap-3">
                  <div className="col-span-2">
                    <FldInput label="Course Name *" value={editForm.courseName} onChange={(v) => setField("courseName", v)} />
                  </div>
                  <div>
                    <Label className="text-xs text-muted-foreground">Degree Level</Label>
                    <Select value={editForm.degreeLevel || "__none__"} onValueChange={(v) => setField("degreeLevel", v === "__none__" ? "" : v)}>
                      <SelectTrigger className="h-8 text-sm mt-1"><SelectValue placeholder="Select level" /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__none__">— None —</SelectItem>
                        {DEGREE_LEVELS.map((l) => <SelectItem key={l} value={l}>{l}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label className="text-xs text-muted-foreground">Category</Label>
                    <Select value={editForm.category || "__none__"} onValueChange={(v) => setField("category", v === "__none__" ? "" : v)}>
                      <SelectTrigger className="h-8 text-sm mt-1"><SelectValue placeholder="Select category" /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__none__">— None —</SelectItem>
                        {CATEGORY_NAMES.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                  <FldInput label="Course Website" value={editForm.courseWebsite} onChange={(v) => setField("courseWebsite", v)} />
                  <FldInput label="Language" value={editForm.language} onChange={(v) => setField("language", v)} />
                  <FldInput label="Duration (number)" value={editForm.duration} onChange={(v) => setField("duration", v)} type="number" />
                  <FldInput label="Duration Term (Years/Semesters)" value={editForm.durationTerm} onChange={(v) => setField("durationTerm", v)} />
                  <div>
                    <Label className="text-xs text-muted-foreground">Study Mode</Label>
                    <Select value={editForm.studyMode || "__none__"} onValueChange={(v) => setField("studyMode", v === "__none__" ? "" : v)}>
                      <SelectTrigger className="h-8 text-sm mt-1"><SelectValue placeholder="Select mode" /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__none__">— None —</SelectItem>
                        {STUDY_MODES.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                  <FldInput label="Study Load" value={editForm.studyLoad} onChange={(v) => setField("studyLoad", v)} />
                  <FldInput label="Location / Campus" value={editForm.courseLocation} onChange={(v) => setField("courseLocation", v)} />
                  <FldInput label="Intake Months (comma-separated)" value={editForm.intakeMonths} onChange={(v) => setField("intakeMonths", v)} />
                </div>
              </div>

              {/* Fee */}
              <div>
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">International Fee</p>
                <div className="grid grid-cols-4 gap-3">
                  <FldInput label="Fee Amount" value={editForm.internationalFee} onChange={(v) => setField("internationalFee", v)} type="number" />
                  <FldInput label="Fee Term" value={editForm.feeTerm} onChange={(v) => setField("feeTerm", v)} />
                  <FldInput label="Fee Year" value={editForm.feeYear} onChange={(v) => setField("feeYear", v)} type="number" />
                  <FldInput label="Currency" value={editForm.currency} onChange={(v) => setField("currency", v)} />
                </div>
              </div>

              {/* IELTS */}
              <div>
                <p className="text-xs font-semibold text-purple-700 uppercase tracking-wide mb-2">IELTS</p>
                <div className="grid grid-cols-5 gap-3">
                  <FldInput label="Overall" value={editForm.ieltsOverall} onChange={(v) => setField("ieltsOverall", v)} type="number" />
                  <FldInput label="Listening" value={editForm.ieltsListening} onChange={(v) => setField("ieltsListening", v)} type="number" />
                  <FldInput label="Speaking" value={editForm.ieltsSpeaking} onChange={(v) => setField("ieltsSpeaking", v)} type="number" />
                  <FldInput label="Writing" value={editForm.ieltsWriting} onChange={(v) => setField("ieltsWriting", v)} type="number" />
                  <FldInput label="Reading" value={editForm.ieltsReading} onChange={(v) => setField("ieltsReading", v)} type="number" />
                </div>
              </div>

              {/* PTE */}
              <div>
                <p className="text-xs font-semibold text-orange-600 uppercase tracking-wide mb-2">PTE</p>
                <div className="grid grid-cols-5 gap-3">
                  <FldInput label="Overall" value={editForm.pteOverall} onChange={(v) => setField("pteOverall", v)} type="number" />
                  <FldInput label="Listening" value={editForm.pteListening} onChange={(v) => setField("pteListening", v)} type="number" />
                  <FldInput label="Speaking" value={editForm.pteSpeaking} onChange={(v) => setField("pteSpeaking", v)} type="number" />
                  <FldInput label="Writing" value={editForm.pteWriting} onChange={(v) => setField("pteWriting", v)} type="number" />
                  <FldInput label="Reading" value={editForm.pteReading} onChange={(v) => setField("pteReading", v)} type="number" />
                </div>
              </div>

              {/* TOEFL */}
              <div>
                <p className="text-xs font-semibold text-rose-600 uppercase tracking-wide mb-2">TOEFL</p>
                <div className="grid grid-cols-5 gap-3">
                  <FldInput label="Overall" value={editForm.toeflOverall} onChange={(v) => setField("toeflOverall", v)} type="number" />
                  <FldInput label="Listening" value={editForm.toeflListening} onChange={(v) => setField("toeflListening", v)} type="number" />
                  <FldInput label="Speaking" value={editForm.toeflSpeaking} onChange={(v) => setField("toeflSpeaking", v)} type="number" />
                  <FldInput label="Writing" value={editForm.toeflWriting} onChange={(v) => setField("toeflWriting", v)} type="number" />
                  <FldInput label="Reading" value={editForm.toeflReading} onChange={(v) => setField("toeflReading", v)} type="number" />
                </div>
              </div>

              {/* Other English */}
              <div>
                <p className="text-xs font-semibold text-pink-600 uppercase tracking-wide mb-2">Other English Tests</p>
                <div className="grid grid-cols-2 gap-3">
                  <FldInput label="Cambridge (CAE) Overall" value={editForm.cambridgeOverall} onChange={(v) => setField("cambridgeOverall", v)} type="number" />
                  <FldInput label="Duolingo Overall" value={editForm.duolingoOverall} onChange={(v) => setField("duolingoOverall", v)} type="number" />
                </div>
              </div>

              {/* Academic */}
              <div>
                <p className="text-xs font-semibold text-cyan-700 uppercase tracking-wide mb-2">Academic Requirements</p>
                <div className="grid grid-cols-2 gap-3">
                  <FldInput label="Academic Level" value={editForm.academicLevel} onChange={(v) => setField("academicLevel", v)} />
                  <FldInput label="Score" value={editForm.academicScore} onChange={(v) => setField("academicScore", v)} type="number" />
                  <FldInput label="Score Type (GPA / WAM / ATAR)" value={editForm.scoreType} onChange={(v) => setField("scoreType", v)} />
                  <FldInput label="Country" value={editForm.academicCountry} onChange={(v) => setField("academicCountry", v)} />
                </div>
              </div>

              {/* Other */}
              <div>
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">Other</p>
                <div className="grid grid-cols-1 gap-3">
                  <div className="space-y-1">
                    <Label className="text-xs text-muted-foreground">Other Requirement</Label>
                    <textarea
                      value={editForm.otherRequirement}
                      onChange={(e) => setField("otherRequirement", e.target.value)}
                      rows={2}
                      className="w-full border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-muted-foreground">Scholarship Info</Label>
                    <textarea
                      value={editForm.scholarship}
                      onChange={(e) => setField("scholarship", e.target.value)}
                      rows={2}
                      className="w-full border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                    />
                  </div>
                </div>
              </div>
            </div>

            <DialogFooter className="gap-2">
              <Button variant="outline" onClick={() => { setEditingCourse(null); setEditForm(null); }}>
                Cancel
              </Button>
              {editingCourse.status === "pending" && (
                <Button
                  variant="default"
                  className="bg-green-600 hover:bg-green-700"
                  disabled={saving || approvingId === editingCourse.id}
                  onClick={async () => {
                    await handleSaveEdit();
                    await handleApprove(editingCourse.id);
                  }}
                >
                  <Upload className="w-4 h-4 mr-1.5" />
                  Save & Import
                </Button>
              )}
              <Button onClick={handleSaveEdit} disabled={saving}>
                {saving ? "Saving…" : "Save Changes"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>

    {/* ── BULK EDIT DIALOG ── */}
    {bulkMode && (() => {
      const courseHasData = (c: typeof allCourses[0]) => {
        if (bulkMode === "english") return !!(c.ieltsOverall || c.ieltsSpeaking || c.ieltsListening || c.ieltsWriting || c.ieltsReading || c.pteOverall || c.toeflOverall);
        if (bulkMode === "academic") return !!(c.academicLevel || c.academicScore);
        if (bulkMode === "scholarships") return !!(c.scholarshipDetails);
        return false;
      };
      const filtered = allCourses.filter((c) => {
        if (bulkSearch && !c.name.toLowerCase().includes(bulkSearch.toLowerCase())) return false;
        if (bulkFilter === "missing") return !courseHasData(c);
        if (bulkFilter === "hasData") return courseHasData(c);
        return true;
      });
      const allFilteredIds = filtered.map((c) => c.id);
      const allSelected = allFilteredIds.length > 0 && allFilteredIds.every((id_) => selectedIds.has(id_));
      const toggleAll = () => {
        if (allSelected) {
          const next = new Set(selectedIds);
          allFilteredIds.forEach((id_) => next.delete(id_));
          setSelectedIds(next);
        } else {
          const next = new Set(selectedIds);
          allFilteredIds.forEach((id_) => next.add(id_));
          setSelectedIds(next);
        }
      };
      const toggle = (id_: number) => {
        const next = new Set(selectedIds);
        if (next.has(id_)) next.delete(id_); else next.add(id_);
        setSelectedIds(next);
      };
      const title = bulkMode === "english" ? "Bulk Edit English Proficiency" : bulkMode === "academic" ? "Bulk Edit Academic Requirements" : "Bulk Add Scholarship";
      const accentColor = bulkMode === "english" ? "#7e22ce" : bulkMode === "academic" ? "#0e7490" : "#b45309";

      return (
        <Dialog open onOpenChange={() => setBulkMode(null)}>
          <DialogContent className="max-w-5xl h-[90vh] flex flex-col p-0 gap-0">
            <DialogHeader className="px-6 pt-5 pb-3 border-b shrink-0">
              <DialogTitle style={{ color: accentColor }}>{title}</DialogTitle>
              <p className="text-xs text-muted-foreground mt-0.5">{selectedIds.size} course{selectedIds.size !== 1 ? "s" : ""} selected · fill in the form and click Apply</p>
            </DialogHeader>
            <div className="flex flex-1 overflow-hidden">
              {/* Left: course selector */}
              <div className="w-72 shrink-0 border-r flex flex-col">
                <div className="p-3 border-b space-y-2">
                  <Input placeholder="Search courses…" value={bulkSearch} onChange={(e) => setBulkSearch(e.target.value)} className="h-8 text-xs" />
                  <div className="flex gap-1">
                    {(["all", "missing", "hasData"] as const).map((f) => (
                      <button key={f} onClick={() => setBulkFilter(f)} className={`flex-1 text-[10px] py-1 rounded border transition-colors ${bulkFilter === f ? "bg-primary text-white border-primary" : "border-gray-200 hover:bg-gray-50"}`}>
                        {f === "all" ? "All" : f === "missing" ? "Missing" : "Has Data"}
                      </button>
                    ))}
                  </div>
                  <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
                    <input type="checkbox" checked={allSelected} onChange={toggleAll} className="rounded" />
                    <span>{allSelected ? "Deselect all" : `Select all (${filtered.length})`}</span>
                  </label>
                </div>
                <div className="flex-1 overflow-y-auto divide-y text-xs">
                  {filtered.length === 0 ? (
                    <p className="text-center text-muted-foreground py-8">No courses match</p>
                  ) : filtered.map((c) => (
                    <label key={c.id} className={`flex items-start gap-2 px-3 py-2 cursor-pointer hover:bg-blue-50/40 ${selectedIds.has(c.id) ? "bg-blue-50/60" : ""}`}>
                      <input type="checkbox" checked={selectedIds.has(c.id)} onChange={() => toggle(c.id)} className="mt-0.5 shrink-0" />
                      <div className="min-w-0">
                        <p className="font-medium text-gray-800 line-clamp-1">{c.name}</p>
                        <p className="text-[10px] text-muted-foreground">{c.degreeLevel ?? "—"}</p>
                        {courseHasData(c) && <span className="inline-flex items-center gap-0.5 text-[9px] text-green-600 font-medium"><CheckCircle2 className="w-2.5 h-2.5" /> has data</span>}
                      </div>
                    </label>
                  ))}
                </div>
              </div>

              {/* Right: form */}
              <div className="flex-1 overflow-y-auto p-5 space-y-5">
                {bulkMode === "english" && (
                  <>
                    <div className="space-y-3">
                      <Label className="text-sm font-semibold">Test Type</Label>
                      <Select value={bEngTestType} onValueChange={setBEngTestType}>
                        <SelectTrigger className="h-9"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="IELTS">IELTS</SelectItem>
                          <SelectItem value="PTE">PTE</SelectItem>
                          <SelectItem value="TOEFL">TOEFL</SelectItem>
                          <SelectItem value="Other">Other</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    {bEngTestType === "Other" && (
                      <div className="space-y-1"><Label className="text-xs text-muted-foreground">Test Name</Label><Input value={bEngTestName} onChange={(e) => setBEngTestName(e.target.value)} placeholder="e.g. Cambridge, Duolingo" /></div>
                    )}
                    <div className="grid grid-cols-3 gap-3">
                      {[["Listening", bEngL, setBEngL], ["Speaking", bEngS, setBEngS], ["Writing", bEngW, setBEngW], ["Reading", bEngR, setBEngR], ["Overall", bEngO, setBEngO]] .map(([label, val, setter]) => (
                        <div key={label as string} className="space-y-1">
                          <Label className="text-xs text-muted-foreground">{label as string}</Label>
                          <Input type="number" step="0.5" value={val as string} onChange={(e) => (setter as (v: string) => void)(e.target.value)} placeholder="—" className="h-9" />
                        </div>
                      ))}
                    </div>
                    <p className="text-xs text-muted-foreground bg-purple-50 border border-purple-100 rounded p-2">
                      This will <strong>replace</strong> any existing {bEngTestType} entry for each selected course.
                    </p>
                  </>
                )}

                {bulkMode === "academic" && (
                  <>
                    <div className="grid grid-cols-2 gap-3">
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">Academic Level</Label>
                        <Select value={bAcadLevel} onValueChange={setBacadLevel}>
                          <SelectTrigger className="h-9"><SelectValue placeholder="Select level" /></SelectTrigger>
                          <SelectContent>
                            {["Bachelor", "Master", "PhD", "Doctor/Doctorate", "Graduate Certificate & Diploma", "Certificate & Diploma", "Associate Degree or Equivalent"].map((l) => (
                              <SelectItem key={l} value={l}>{l}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">Score</Label>
                        <Input type="number" value={bAcadScore} onChange={(e) => setBacadScore(e.target.value)} placeholder="e.g. 65" className="h-9" />
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">Score Type</Label>
                        <Select value={bAcadScoreType} onValueChange={(v) => { setBacadScoreType(v); if (!["GPA","CGPA"].includes(v)) setBacadOutOf(""); }}>
                          <SelectTrigger className="h-9"><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {["%", "GPA", "CGPA", "WAM", "ATAR", "Other"].map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">Out of <span className="text-gray-400 font-normal">(e.g. 4, 5, 7)</span></Label>
                        <Input
                          type="number"
                          min="1"
                          step="0.5"
                          value={bAcadOutOf}
                          onChange={(e) => setBacadOutOf(e.target.value)}
                          placeholder={["GPA","CGPA"].includes(bAcadScoreType) ? "e.g. 4" : "optional"}
                          className="h-9"
                        />
                        {bAcadOutOf && (
                          <p className="text-[10px] text-muted-foreground">Will save as: <strong>{bAcadScoreType}/{bAcadOutOf}</strong></p>
                        )}
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">Country</Label>
                        <Popover open={bAcadCountryOpen} onOpenChange={setBacadCountryOpen}>
                          <PopoverTrigger asChild>
                            <Button
                              variant="outline"
                              role="combobox"
                              aria-expanded={bAcadCountryOpen}
                              className="w-full h-9 justify-between font-normal text-sm"
                            >
                              <span className={bAcadCountry ? "text-foreground" : "text-muted-foreground"}>
                                {bAcadCountry || "Select country…"}
                              </span>
                              <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                            </Button>
                          </PopoverTrigger>
                          <PopoverContent className="w-[280px] p-0" align="start">
                            <Command>
                              <CommandInput placeholder="Search country…" />
                              <CommandList>
                                <CommandEmpty>No country found.</CommandEmpty>
                                <CommandGroup>
                                  <CommandItem
                                    value=""
                                    onSelect={() => { setBacadCountry(""); setBacadCountryOpen(false); }}
                                  >
                                    <Check className={`mr-2 h-4 w-4 ${!bAcadCountry ? "opacity-100" : "opacity-0"}`} />
                                    <span className="text-muted-foreground">— None —</span>
                                  </CommandItem>
                                  {COUNTRIES.map((c) => (
                                    <CommandItem
                                      key={c}
                                      value={c}
                                      onSelect={(val) => { setBacadCountry(val === bAcadCountry ? "" : val); setBacadCountryOpen(false); }}
                                    >
                                      <Check className={`mr-2 h-4 w-4 ${bAcadCountry === c ? "opacity-100" : "opacity-0"}`} />
                                      {c}
                                    </CommandItem>
                                  ))}
                                </CommandGroup>
                              </CommandList>
                            </Command>
                          </PopoverContent>
                        </Popover>
                      </div>
                    </div>
                    <p className="text-xs text-muted-foreground bg-cyan-50 border border-cyan-100 rounded p-2">
                      This will <strong>replace</strong> any existing academic requirement for each selected course.
                    </p>
                  </>
                )}

                {bulkMode === "scholarships" && (
                  <>
                    <div className="space-y-3">
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">Scholarship Name <span className="text-red-500">*</span></Label>
                        <Input value={bSchName} onChange={(e) => setBSchName(e.target.value)} placeholder="e.g. Merit Scholarship" className="h-9" />
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">Details</Label>
                        <textarea value={bSchDetails} onChange={(e) => setBSchDetails(e.target.value)} rows={2} placeholder="Scholarship description…" className="w-full border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring" />
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">Eligibility Criteria</Label>
                        <textarea value={bSchEligibility} onChange={(e) => setBSchEligibility(e.target.value)} rows={2} placeholder="Who is eligible…" className="w-full border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring" />
                      </div>
                      <div className="grid grid-cols-2 gap-3">
                        <div className="space-y-1">
                          <Label className="text-xs text-muted-foreground">Amount</Label>
                          <Input type="number" value={bSchAmount} onChange={(e) => setBSchAmount(e.target.value)} placeholder="e.g. 5000" className="h-9" />
                        </div>
                        <div className="space-y-1">
                          <Label className="text-xs text-muted-foreground">Currency</Label>
                          <Select value={bSchCurrency} onValueChange={setBSchCurrency}>
                            <SelectTrigger className="h-9"><SelectValue /></SelectTrigger>
                            <SelectContent>
                              {["AUD", "GBP", "USD", "NZD", "CAD", "EUR"].map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                            </SelectContent>
                          </Select>
                        </div>
                      </div>
                      <label className="flex items-center gap-2 text-xs cursor-pointer">
                        <input type="checkbox" checked={bSchReplace} onChange={(e) => setBSchReplace(e.target.checked)} />
                        Replace existing scholarships on selected courses (instead of adding alongside)
                      </label>
                    </div>
                  </>
                )}
              </div>
            </div>

            <DialogFooter className="px-6 py-3 border-t shrink-0">
              <Button variant="outline" onClick={() => setBulkMode(null)}>Cancel</Button>
              <Button
                disabled={selectedIds.size === 0 || bulkApplying || (bulkMode === "scholarships" && !bSchName)}
                onClick={applyBulk}
                style={{ backgroundColor: accentColor }}
                className="text-white"
              >
                {bulkApplying ? "Applying…" : `Apply to ${selectedIds.size} Course${selectedIds.size !== 1 ? "s" : ""}`}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      );
    })()}

    </>
  );
}
