import type { AroundNavHandler } from "wouter";

export const COURSE_EDIT_LEAVE_MESSAGE = "You have unsaved course edits. Leave this page and discard them?";
export const COURSE_REPORT_LEAVE_MESSAGE = "You have an unfinished course recovery report. Leave this page and discard it?";

let hasUnsavedCourseEdits: (() => boolean) | null = null;
const reportChecks = new Map<() => boolean, "history" | "review">();

export function registerCourseReportNavigationGuard(check: () => boolean, scope: "history" | "review"): () => void {
  reportChecks.set(check, scope);
  return () => { reportChecks.delete(check); };
}

export function hasUnsavedCourseReports(): boolean {
  return [...reportChecks.keys()].some(check => check());
}

export function confirmHistoryReportDiscard(): boolean {
  return ![...reportChecks].some(([check, scope]) => scope === "history" && check())
    || window.confirm(COURSE_REPORT_LEAVE_MESSAGE);
}

export function registerCourseEditNavigationGuard(check: () => boolean): () => void {
  hasUnsavedCourseEdits = check;
  return () => {
    if (hasUnsavedCourseEdits === check) hasUnsavedCourseEdits = null;
  };
}

export function confirmCourseEditNavigation(): boolean {
  if (hasUnsavedCourseEdits?.() && !window.confirm(COURSE_EDIT_LEAVE_MESSAGE)) return false;
  return !hasUnsavedCourseReports() || window.confirm(COURSE_REPORT_LEAVE_MESSAGE);
}

export const aroundCourseEditNavigation: AroundNavHandler = (navigate, to, options) => {
  if (new URL(to, window.location.href).href === window.location.href || confirmCourseEditNavigation()) {
    navigate(to, options);
  }
};