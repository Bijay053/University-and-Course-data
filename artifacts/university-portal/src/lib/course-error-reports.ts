import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchWithAuth } from "@/lib/api";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
export const REPORTS_URL = `${BASE}/api/course-error-reports`;

export const REPORT_KINDS = [
  { value: "missing", label: "Missing information" },
  { value: "incorrect", label: "Incorrect information" },
] as const;
export const REPORT_FIELDS = [
  { value: "fees", label: "Fees" },
  { value: "english", label: "English requirements" },
  { value: "intakes", label: "Intakes" },
  { value: "location", label: "Location" },
  { value: "duration", label: "Duration" },
  { value: "scholarships", label: "Scholarships" },
  { value: "other", label: "Other" },
  { value: "key_insights", label: "Key Insights" },
] as const;
export type ReportKind = (typeof REPORT_KINDS)[number]["value"];
export type ReportField = (typeof REPORT_FIELDS)[number]["value"];
export type ReportStatus = "new" | "reviewed" | "resolved";

export type CourseErrorReportInput = {
  course_id?: number;
  university_id?: number;
  insight_id?: number;
  kind: ReportKind;
  field: ReportField;
  message: string;
  source_url?: string;
  website?: string;
};
export type CourseErrorReport = {
  id: number;
  course_id: number | null;
  course_name: string | null;
  university_id?: number | null;
  insight_id?: number | null;
  insight_country?: string | null;
  university_name: string | null;
  kind: ReportKind;
  field: ReportField;
  message: string;
  source_url: string | null;
  status: ReportStatus;
  created_at: string;
  updated_at: string;
};
export type ReportList = { reports: CourseErrorReport[]; total: number };

export class ReportApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

async function readError(res: Response): Promise<ReportApiError> {
  let msg = res.status === 429 ? "Too many reports. Please wait a moment and try again." : `Request failed (${res.status}).`;
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") msg = body.detail;
    else if (Array.isArray(body?.detail)) msg = body.detail.map((d: { msg?: string }) => d.msg ?? "Invalid field").join("; ");
  } catch { /* keep default */ }
  return new ReportApiError(msg, res.status);
}

export function validateReport(v: { message: string; source_url: string }): Partial<Record<"message" | "source_url", string>> {
  const errors: Partial<Record<"message" | "source_url", string>> = {};
  const m = v.message.trim();
  if (m.length < 10) errors.message = "Please describe the problem in at least 10 characters.";
  else if (m.length > 2000) errors.message = "Please keep the description under 2000 characters.";
  const u = v.source_url.trim();
  if (u) {
    try {
      const p = new URL(u);
      if (p.protocol !== "http:" && p.protocol !== "https:") throw new Error();
    } catch { errors.source_url = "Enter a full link starting with http:// or https://"; }
  }
  return errors;
}

export async function submitCourseErrorReport(input: CourseErrorReportInput): Promise<{ id: number; status: "new" }> {
  const res = await fetch(REPORTS_URL, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await readError(res);
  return res.json();
}

export function useSubmitCourseErrorReport() {
  return useMutation({ mutationFn: submitCourseErrorReport });
}

export const reportsKey = (status: ReportStatus | "all", offset: number) => ["course-error-reports", status, offset] as const;

export function useCourseErrorReports(status: ReportStatus | "all", offset: number, enabled: boolean, limit = 20) {
  return useQuery({
    queryKey: reportsKey(status, offset),
    enabled,
    retry: (count, err) => !(err instanceof ReportApiError && (err.status === 401 || err.status === 403)) && count < 2,
    queryFn: async (): Promise<ReportList> => {
      const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
      if (status !== "all") qs.set("status", status);
      const res = await fetchWithAuth(`${REPORTS_URL}?${qs}`);
      if (!res.ok) throw await readError(res);
      const body = await res.json();
      return { reports: Array.isArray(body?.reports) ? body.reports : [], total: Number(body?.total) || 0 };
    },
  });
}

export function useUpdateReportStatus() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, status }: { id: number; status: ReportStatus }): Promise<CourseErrorReport> => {
      const res = await fetchWithAuth(`${REPORTS_URL}/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      if (!res.ok) throw await readError(res);
      return res.json();
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["course-error-reports"] }),
  });
}
