import { describe, expect, it, vi } from "vitest";
import {
  hasUnsavedCourseReports,
  registerCourseReportNavigationGuard,
  subscribeCourseReportGuardChanges,
} from "./course-edit-navigation-guard";

const flush = () => new Promise<void>(resolve => queueMicrotask(resolve));

describe("course report guard subscription", () => {
  it("notifies on register and unregister, then stops after unsubscribe", async () => {
    const listener = vi.fn();
    const unsubscribe = subscribeCourseReportGuardChanges(listener);
    const unregister = registerCourseReportNavigationGuard(() => true, "review");
    await flush();
    expect(listener).toHaveBeenCalledTimes(1);
    expect(hasUnsavedCourseReports()).toBe(true);
    unregister();
    await flush();
    expect(listener).toHaveBeenCalledTimes(2);
    expect(hasUnsavedCourseReports()).toBe(false);
    unsubscribe();
    registerCourseReportNavigationGuard(() => false, "review")();
    await flush();
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("coalesces an effect-style re-registration so listeners never see a transient clean gap", async () => {
    const seen: boolean[] = [];
    const unsubscribe = subscribeCourseReportGuardChanges(() => seen.push(hasUnsavedCourseReports()));
    const first = registerCourseReportNavigationGuard(() => true, "review");
    await flush();
    // React effect re-run: cleanup then register in the same synchronous commit.
    first();
    const second = registerCourseReportNavigationGuard(() => true, "review");
    await flush();
    expect(seen).toEqual([true, true]);
    second();
    await flush();
    expect(seen).toEqual([true, true, false]);
    unsubscribe();
  });
});
