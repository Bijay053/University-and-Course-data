import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, ShieldAlert } from "lucide-react";

export type LiveProbeSample = {
  url: string;
  classification: string;
  reason: string;
};

export type LiveProbe = {
  status?: string;
  pages_checked?: number;
  course_pages?: number;
  rejected_pages?: number;
  failures?: number;
  reason?: string;
  samples?: LiveProbeSample[];
};

export type AutonomousRepair = {
  enabled: true;
  phase:
    | "queued"
    | "live_probe"
    | "repairing"
    | "validating"
    | "verification_queued"
    | "verifying"
    | "verified"
    | "needs_review"
    | "blocked";
  limits?: {
    max_attempts?: number;
    max_live_pages?: number;
    max_live_seconds?: number;
    max_verification_runs?: number;
    [key: string]: unknown;
  };
  verification_limits?: {
    max_courses?: number;
    time_budget_seconds?: number;
    cost_cap_usd?: number;
    scope?: string;
    selected_courses?: number;
    staged_courses?: number;
    budget_exhausted?: string;
    warning?: string;
  };
  verification_job_id?: string;
  verification_status?: string;
  comparison?: {
    baseline?: VerificationQuality;
    verification?: VerificationQuality;
    baseline_quality?: VerificationQuality;
    verification_quality?: VerificationQuality;
    unresolved_fields?: string[] | Record<string, unknown>;
    regressions?: string[] | Record<string, unknown>;
    capped?: boolean;
    stop_reason?: string | null;
    verification_limits?: AutonomousRepair["verification_limits"];
    counters?: {
      total_found?: number;
      current?: number;
      imported?: number;
      skipped?: number;
      errors?: number;
    };
    [key: string]: unknown;
  };
  reason?: string;
};

type VerificationQuality = Partial<Record<
  "fee_pct" | "ielts_pct" | "duration_pct" | "location_pct" | "course_name_pct",
  number
>>;

const QUALITY_FIELDS: Array<{ key: keyof VerificationQuality; label: string }> = [
  { key: "fee_pct", label: "Fees" },
  { key: "ielts_pct", label: "IELTS" },
  { key: "duration_pct", label: "Duration" },
  { key: "location_pct", label: "Location" },
  { key: "course_name_pct", label: "Course names" },
];

const PHASES: Array<{ phase: AutonomousRepair["phase"]; label: string }> = [
  { phase: "live_probe", label: "Probe official sources" },
  { phase: "repairing", label: "Propose repairs" },
  { phase: "validating", label: "Test repair" },
  { phase: "verification_queued", label: "Save config" },
  { phase: "verifying", label: "Verification scrape" },
  { phase: "verified", label: "Sample verified" },
];

function phaseIndex(phase: AutonomousRepair["phase"]) {
  if (phase === "queued") return -1;
  if (phase === "needs_review" || phase === "blocked") return 5;
  return PHASES.findIndex(item => item.phase === phase);
}

export function AiRepairProgress({
  autonomous,
  liveProbe,
  currentAttempt,
  maxAttempts = 5,
  onOpenVerificationJob,
}: {
  autonomous: AutonomousRepair;
  liveProbe?: LiveProbe;
  currentAttempt: number;
  maxAttempts?: number;
  onOpenVerificationJob?: (jobId: string) => void;
}) {
  const currentIndex = phaseIndex(autonomous.phase);
  const verified = autonomous.phase === "verified";
  const needsReview = autonomous.phase === "needs_review";
  const blocked = autonomous.phase === "blocked";
  const running = !verified && !needsReview && !blocked;
  const effectiveMaxAttempts = autonomous.limits?.max_attempts ?? maxAttempts;
  const comparison = autonomous.comparison;
  const verificationLimits = comparison?.verification_limits ?? autonomous.verification_limits;
  const baseline = comparison?.baseline ?? comparison?.baseline_quality;
  const verification = comparison?.verification ?? comparison?.verification_quality;
  const displayComparisonItem = (key: string) => (
    key.replace(/_pct$/, "").replace(/_/g, " ")
  );
  const comparisonItems = (value: string[] | Record<string, unknown> | undefined) => {
    if (Array.isArray(value)) return value.map(displayComparisonItem);
    if (!value) return [];
    return Object.entries(value)
      .filter(([, item]) => Boolean(item))
      .map(([key]) => displayComparisonItem(key));
  };
  const unresolvedFields = comparisonItems(comparison?.unresolved_fields);
  const regressions = comparisonItems(comparison?.regressions);
  const stoppedForTime = (
    comparison?.stop_reason === "time_budget_exhausted"
    || verificationLimits?.budget_exhausted === "time_budget_exhausted"
  );
  const processedCourses = comparison?.counters?.current;
  const selectedCourses = verificationLimits?.selected_courses ?? comparison?.counters?.total_found;
  const stagedCourses = verificationLimits?.staged_courses ?? comparison?.counters?.imported;

  return (
    <section
      aria-label="One-click AI repair progress"
      className={`rounded-lg border p-2.5 space-y-2 ${
        verified
          ? "border-emerald-200 bg-emerald-50"
          : blocked
            ? "border-red-200 bg-red-50"
            : needsReview
              ? "border-amber-200 bg-amber-50"
              : "border-violet-200 bg-violet-50"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-1.5 text-[10px] font-semibold">
            {verified ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
            ) : blocked ? (
              <ShieldAlert className="h-3.5 w-3.5 text-red-600" />
            ) : needsReview ? (
              <AlertTriangle className="h-3.5 w-3.5 text-amber-600" />
            ) : (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-violet-600" />
            )}
            {verified ? "Bounded sample verified" : needsReview ? "Repair needs review" : blocked ? "Repair blocked" : "Automatic repair running"}
          </div>
          <p className="mt-0.5 text-[9px] text-gray-600">
            {verified
              ? "The saved config passed a bounded verification sample; full catalogue coverage is not certified."
              : needsReview
                ? "The result was not verified automatically. Review the evidence and staged courses."
                : blocked
                  ? "No config was marked as verified. Manual investigation is required."
                  : "Testing bounded changes against live official sources."}
          </p>
        </div>
        <span className="shrink-0 rounded-full border bg-white px-1.5 py-0.5 text-[9px] font-medium text-gray-600">
          Attempt {Math.min(currentAttempt, effectiveMaxAttempts)}/{effectiveMaxAttempts}
        </span>
      </div>

      <ol className="grid grid-cols-3 gap-1 sm:grid-cols-6" aria-label="Repair stages">
        {PHASES.map((item, index) => {
          const complete = currentIndex > index || verified;
          const active = currentIndex === index && running;
          return (
            <li
              key={item.phase}
              aria-current={active ? "step" : undefined}
              className={`rounded border px-1 py-1 text-center text-[8px] leading-tight ${
                complete
                  ? "border-emerald-200 bg-emerald-100 text-emerald-800"
                  : active
                    ? "border-violet-300 bg-white font-semibold text-violet-800"
                    : "border-gray-200 bg-white/70 text-gray-400"
              }`}
            >
              {item.label}
            </li>
          );
        })}
      </ol>

      {liveProbe && (
        <div className="rounded border border-blue-200 bg-white/80 p-2 text-[9px] text-gray-700">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <strong className="text-blue-800">Live evidence: {liveProbe.status ?? "pending"}</strong>
            <span>{liveProbe.pages_checked ?? 0} pages checked</span>
            <span>{liveProbe.course_pages ?? 0} course pages</span>
            <span>{liveProbe.rejected_pages ?? 0} rejected</span>
            {(liveProbe.failures ?? 0) > 0 && <span className="text-red-700">{liveProbe.failures} failures</span>}
          </div>
          {liveProbe.reason && <p className="mt-1 text-gray-600">{liveProbe.reason}</p>}
          {(liveProbe.samples?.length ?? 0) > 0 && (
            <ul className="mt-1 space-y-0.5">
              {liveProbe.samples!.slice(0, 3).map(sample => (
                <li key={sample.url} className="flex min-w-0 items-center gap-1">
                  <a
                    href={sample.url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex min-w-0 items-center gap-0.5 truncate font-medium text-blue-700 underline"
                    title={sample.url}
                  >
                    Official source <ExternalLink className="h-2.5 w-2.5 shrink-0" />
                  </a>
                  <span className="shrink-0">· {sample.classification}</span>
                  {sample.reason && <span className="truncate text-gray-500">· {sample.reason}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-1 text-[9px] text-gray-500">
        <span>
          Limits: {autonomous.limits?.max_live_pages ?? 12} live pages · {autonomous.limits?.max_live_seconds ?? 180}s ·{" "}
          {autonomous.limits?.max_verification_runs ?? 1} verification run
        </span>
        {autonomous.verification_status && <span>Verification: {autonomous.verification_status.replace(/_/g, " ")}</span>}
      </div>

      {verificationLimits && (
        <div className="rounded border border-indigo-200 bg-white/80 px-2 py-1.5 text-[9px] text-indigo-900">
          <strong>Verification limit:</strong>{" "}
          up to {verificationLimits.max_courses ?? 50} courses ·{" "}
          {Math.round((verificationLimits.time_budget_seconds ?? 600) / 60)} minutes
          {verificationLimits.cost_cap_usd != null && (
            <> · Gemini extraction ceiling: ${verificationLimits.cost_cap_usd.toFixed(2)}</>
          )}
          <p className="mt-0.5 text-indigo-700">
            {verificationLimits.scope ?? "Bounded fresh catalogue verification; not full catalogue coverage."}
          </p>
        </div>
      )}

      {(baseline || verification) && (
        <div className="overflow-hidden rounded border border-gray-200 bg-white/80 text-[9px]">
          <div className="grid grid-cols-[1fr_52px_64px] gap-1 border-b border-gray-200 bg-gray-50 px-2 py-1 font-semibold text-gray-600">
            <span>Quality</span><span>Baseline</span><span>Verification</span>
          </div>
          {QUALITY_FIELDS.map(({ key, label }) => (
            <div key={key} className="grid grid-cols-[1fr_52px_64px] gap-1 border-b border-gray-100 px-2 py-1 last:border-0">
              <span>{label}</span>
              <span>{baseline?.[key] != null ? `${baseline[key]}%` : "—"}</span>
              <span>{verification?.[key] != null ? `${verification[key]}%` : "—"}</span>
            </div>
          ))}
        </div>
      )}

      {comparison && (unresolvedFields.length > 0 || regressions.length > 0 || comparison.capped || stoppedForTime) && (
        <div className="space-y-0.5 rounded border border-amber-200 bg-white/70 px-2 py-1.5 text-[9px] text-amber-900">
          {unresolvedFields.length > 0 && <p><strong>Unresolved fields:</strong> {unresolvedFields.join(", ")}</p>}
          {regressions.length > 0 && <p><strong>Regressions:</strong> {regressions.join(", ")}</p>}
          {stoppedForTime && (
            <p>
              <strong>Stopped:</strong> Verification reached its{" "}
              {Math.round((verificationLimits?.time_budget_seconds ?? 600) / 60)}-minute time budget
              {processedCourses != null && selectedCourses != null
                ? ` after processing ${processedCourses} of ${selectedCourses} selected courses`
                : ""}
              {stagedCourses != null ? ` and staging ${stagedCourses}` : ""}.
            </p>
          )}
          {comparison.capped && !stoppedForTime && (
            <p><strong>Capped:</strong> Verification stopped at its {verificationLimits?.max_courses ?? 50}-course limit.</p>
          )}
        </div>
      )}

      {(autonomous.reason || (autonomous.phase === "verification_queued" && !autonomous.verification_job_id)) && (
        <p className={`rounded border bg-white/70 px-2 py-1 text-[9px] ${blocked ? "border-red-200 text-red-800" : "border-amber-200 text-amber-800"}`}>
          {autonomous.reason ?? "Config saved. Waiting for the automatic verification scrape; this is not verified yet."}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[9px] font-medium text-gray-600">
          Courses remain staged for review; publishing is always manual.
        </span>
        {autonomous.verification_job_id && onOpenVerificationJob && (
          <button
            type="button"
            onClick={() => onOpenVerificationJob(autonomous.verification_job_id!)}
            className="text-[9px] font-semibold text-blue-700 underline underline-offset-2 hover:text-blue-900"
          >
            Open verification job →
          </button>
        )}
      </div>
    </section>
  );
}