import type { AroundNavHandler } from "wouter";

export const COURSE_EDIT_LEAVE_MESSAGE = "You have unsaved course edits. Leave this page and discard them?";
export const COURSE_REPORT_LEAVE_MESSAGE = "You have an unfinished course recovery report. Leave this page and discard it?";

let hasUnsavedCourseEdits: (() => boolean) | null = null;
let discardCourseEdit: (() => void) | null = null;
const reportChecks = new Map<() => boolean, "history" | "review">();

const reportGuardListeners = new Set<() => void>();
let reportGuardNotifyQueued = false;

/**
 * Coalesced, deferred to a microtask so a React effect re-registration
 * (cleanup + register in one commit) is observed as one settled state,
 * never as a transient "no draft" gap.
 */
function notifyReportGuardChange(): void {
  if (reportGuardNotifyQueued) return;
  reportGuardNotifyQueued = true;
  queueMicrotask(() => {
    reportGuardNotifyQueued = false;
    [...reportGuardListeners].forEach(listener => listener());
  });
}

/** Subscribe to report-draft guard registration changes. Returns an unsubscribe function. */
export function subscribeCourseReportGuardChanges(listener: () => void): () => void {
  reportGuardListeners.add(listener);
  return () => { reportGuardListeners.delete(listener); };
}

export function registerCourseReportNavigationGuard(check: () => boolean, scope: "history" | "review"): () => void {
  reportChecks.set(check, scope);
  notifyReportGuardChange();
  return () => {
    reportChecks.delete(check);
    notifyReportGuardChange();
  };
}

export function hasUnsavedCourseReports(): boolean {
  return [...reportChecks.keys()].some(check => check());
}

export function confirmHistoryReportDiscard(): boolean {
  return ![...reportChecks].some(([check, scope]) => scope === "history" && check())
    || window.confirm(COURSE_REPORT_LEAVE_MESSAGE);
}

export function registerCourseEditNavigationGuard(check: () => boolean, onDiscard?: () => void): () => void {
  hasUnsavedCourseEdits = check;
  discardCourseEdit = onDiscard ?? null;
  return () => {
    if (hasUnsavedCourseEdits === check) {
      hasUnsavedCourseEdits = null;
      discardCourseEdit = null;
    }
  };
}

export function confirmCourseEditNavigation(): boolean {
  if (hasUnsavedCourseEdits?.() && !window.confirm(COURSE_EDIT_LEAVE_MESSAGE)) return false;
  if (hasUnsavedCourseReports() && !window.confirm(COURSE_REPORT_LEAVE_MESSAGE)) return false;
  if (hasUnsavedCourseEdits?.()) discardCourseEdit?.();
  return true;
}

export const aroundCourseEditNavigation: AroundNavHandler = (navigate, to, options) => {
  if (new URL(to, window.location.href).href === window.location.href || confirmCourseEditNavigation()) {
    navigate(to, options);
  }
};