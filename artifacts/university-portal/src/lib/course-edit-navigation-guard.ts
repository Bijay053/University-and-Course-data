import type { AroundNavHandler } from "wouter";

export const COURSE_EDIT_LEAVE_MESSAGE = "You have unsaved course edits. Leave this page and discard them?";

let hasUnsavedCourseEdits: (() => boolean) | null = null;

export function registerCourseEditNavigationGuard(check: () => boolean): () => void {
  hasUnsavedCourseEdits = check;
  return () => {
    if (hasUnsavedCourseEdits === check) hasUnsavedCourseEdits = null;
  };
}

export function confirmCourseEditNavigation(): boolean {
  return !hasUnsavedCourseEdits?.() || window.confirm(COURSE_EDIT_LEAVE_MESSAGE);
}

export const aroundCourseEditNavigation: AroundNavHandler = (navigate, to, options) => {
  if (new URL(to, window.location.href).href === window.location.href || confirmCourseEditNavigation()) {
    navigate(to, options);
  }
};