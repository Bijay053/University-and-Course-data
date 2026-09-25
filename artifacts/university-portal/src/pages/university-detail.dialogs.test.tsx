// @vitest-environment jsdom

import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import UniversityDetail from "./university-detail";
import { assertOpenDialogsHaveAccessibleContext } from "@/test/dialog-accessibility";

const toastMock = vi.fn();

const course = {
  id: 42,
  name: "Accessible Course",
  degreeLevel: "Bachelor",
  ieltsListening: 6,
  ieltsSpeaking: 6,
  ieltsWriting: 6,
  ieltsReading: 6,
  ieltsOverall: 6.5,
};

vi.mock("@workspace/api-client-react", () => ({
  getGetUniversityQueryKey: (id: number) => ["university", id],
  getListCoursesQueryKey: () => ["courses"],
  useGetUniversity: () => ({
    data: { id: 7, name: "Accessible University", city: "Sydney", country: "Australia", website: "https://example.edu" },
    isLoading: false,
  }),
  useListCourses: () => ({
    data: { data: [course], total: 1 },
    isLoading: false,
  }),
}));

vi.mock("wouter", async () => {
  const actual = await vi.importActual<typeof import("wouter")>("wouter");
  return {
    ...actual,
    useRoute: () => [true, { id: "7" }],
    useLocation: () => ["/universities/7", vi.fn()],
  };
});

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: toastMock }),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type ApprovalResult = {
  approvedIds: number[]; approvedCount: number; splitCount: number;
  failed: Array<{ id: number; error: string }>; attempted: number;
};
function renderPage(approval?: (sourceId: number, force: boolean) => ApprovalResult) {
  const approvedSources = new Set<number>();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/scrape/staged/approve-selected")) {
      const { courseIds, force } = JSON.parse(String(init?.body));
      const response = approval?.(courseIds[0], force);
      if (response?.approvedIds.includes(courseIds[0])) approvedSources.add(courseIds[0]);
      return new Response(JSON.stringify(response), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    const body = url.includes("/scholarship-courses")
      ? [{ id: 42, name: "Accessible Course", degreeLevel: "Bachelor", category: "Business", scholarships: [{ id: 8, name: "Merit Award", details: "For strong applicants", eligibilityCriteria: "International students", amount: 5000, percentage: null, currency: "AUD" }] }]
      : url.includes("/academic-requirements")
      ? [{ id: 9, courseId: 42, courseName: "Accessible Course", degreeLevel: "Bachelor", academicLevelOptionId: 1, academicLevel: "Year 12", academicScore: 75, scoreType: "%", academicCountry: "Australia" }]
      : url.includes("/assessment-notes")
      ? [{ id: 10, country: "Australia", raw_text: "Representative assessment note", parsed_data: null, created_at: "2026-09-03T00:00:00Z" }]
      : url.includes("/locations")
      ? [{ id: 11, universityId: 7, displayName: "City Campus", fullAddress: "1 Campus Way", city: "Sydney", stateRegion: "NSW", country: "Australia", latitude: -33.86, longitude: 151.2, courseCount: 1, isVerified: true }]
      : url.includes("/scrape/staged")
      ? (approval ? [
          { id: 12, course_name: "Staged Accessible Course", status: "pending", completeness: 45 },
          { id: 13, course_name: "Campus-fee Course", status: "pending", completeness: 70 },
        ].filter(c => !approvedSources.has(c.id)) : [{ id: 12, course_name: "Staged Accessible Course", status: "pending", completeness: 45 }])
      : url.includes("/repair/missing/")
      ? { courses: [] }
      : url.includes("/change-detection/")
        ? { summary: { total: 0 }, events: [] }
        : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <UniversityDetail />
    </QueryClientProvider>,
  );
  return { ...view, fetchMock };
}

async function openTab(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("button", { name: new RegExp(name, "i") }));
}

function expectAccessibleDialog() {
  expect(() => assertOpenDialogsHaveAccessibleContext()).not.toThrow();
}

async function expectDialogAndClose(
  user: ReturnType<typeof userEvent.setup>,
  name: string | RegExp,
) {
  await screen.findByRole("dialog", { name });
  expectAccessibleDialog();
  await user.keyboard("{Escape}");
}

describe("University Detail dialogs", () => {
  it("approves all selected IDs in one request, retaining ambiguous campus rows", async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderPage(sourceId => sourceId === 12
      ? { approvedIds: [12, 32], approvedCount: 2, splitCount: 1, failed: [], attempted: 1 }
      : { approvedIds: [], approvedCount: 0, splitCount: 0, failed: [{ id: 13, error: "Campus mapping is ambiguous" }], attempted: 1 });
    await openTab(user, "Raw Data");
    await screen.findByText("Campus-fee Course");
    await user.click(screen.getByTitle("Select all"));
    await user.click(screen.getByRole("button", { name: "Approve (2)" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/scrape/staged/approve-selected"))).toHaveLength(2));
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/scrape/staged/approve-selected"));
    expect(calls.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { courseIds: [12], force: false }, { courseIds: [13], force: false },
    ]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve (1)" })).toBeTruthy());
    expect(screen.getByText("Campus-fee Course")).toBeTruthy();
    expect(screen.queryByText("Staged Accessible Course")).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/12/approve"))).toBe(false);
  });

  it("preserves force approval confirmation while batching all selected IDs", async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderPage(sourceId => ({
      approvedIds: [sourceId], approvedCount: 1, splitCount: 0, failed: [], attempted: 1,
    }));
    await openTab(user, "Raw Data");
    await screen.findByText("Campus-fee Course");
    await user.click(screen.getByTitle("Select all"));
    await user.click(screen.getByRole("button", { name: "Force Approve (2)" }));
    const dialog = await screen.findByRole("dialog", { name: "Force Approve 2 Courses" });
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/approve-selected"))).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Force Approve 2" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/approve-selected"))).toHaveLength(2));
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/approve-selected"));
    expect(calls.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { courseIds: [12], force: true }, { courseIds: [13], force: true },
    ]);
  });

  it("forces only the confidence-blocked source on retry, never repeating successful campus groups", async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderPage((sourceId, force) => sourceId === 12 || force
      ? { approvedIds: sourceId === 12 ? [12, 32] : [13], approvedCount: sourceId === 12 ? 2 : 1, splitCount: sourceId === 12 ? 1 : 0, failed: [], attempted: 1 }
      : { approvedIds: [], approvedCount: 0, splitCount: 0, failed: [{ id: 13, error: "Confidence is too low" }], attempted: 1 });
    await openTab(user, "Raw Data");
    await screen.findByText("Campus-fee Course");
    await user.click(screen.getByTitle("Select all"));
    await user.click(screen.getByRole("button", { name: "Approve (2)" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve (1)" })).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Force Approve (1)" }));
    const dialog = await screen.findByRole("dialog", { name: "Force Approve 1 Course" });
    await user.click(within(dialog).getByRole("button", { name: "Force Approve 1" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/approve-selected"))).toHaveLength(3));
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/approve-selected"));
    expect(calls.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { courseIds: [12], force: false },
      { courseIds: [13], force: false },
      { courseIds: [13], force: true },
    ]);
  });

  it("opens the university edit and repair confirmation dialogs", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByTitle("Edit university"));
    const editDialog = await screen.findByRole("dialog", {
      name: "Edit University",
      description: /update the institution name/i,
    });
    await user.click(within(editDialog).getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "Repair Scrape" }));
    await screen.findByRole("dialog", {
      name: "Repair Scrape — Accessible University",
      description: /review courses with missing critical fields/i,
    });
  });

  it("opens the approved-course delete confirmation dialog", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByTitle("Delete course"));
    await screen.findByRole("dialog", {
      name: "Delete approved course?",
      description: /confirm permanent removal of this approved course/i,
    });
  });

  it("opens Assessment dialogs through visible triggers", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(user, "Key Insights");

    await user.click(await screen.findByRole("button", { name: "Add Key Insight" }));
    await screen.findByRole("dialog", { name: "Add Key Insight" });
    expectAccessibleDialog();
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await user.click(await screen.findByTitle("Edit note"));
    await screen.findByRole("dialog", { name: "Edit Key Insight" });
    expectAccessibleDialog();
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await user.click(await screen.findByTitle("Delete note"));
    await screen.findByRole("dialog", { name: "Delete Note" });
    expectAccessibleDialog();
  });

  it("opens English, Academic, Scholarship, and Location dialogs", async () => {
    const user = userEvent.setup();
    renderPage();

    await openTab(user, "English Proficiency");
    await user.click((await screen.findAllByTitle("Edit"))[0]);
    await expectDialogAndClose(user, /Edit English Proficiency/);
    await user.click((await screen.findAllByTitle("Delete"))[0]);
    await expectDialogAndClose(user, "Delete English Requirements");
    await user.click(screen.getByRole("button", { name: "Bulk Edit English" }));
    await expectDialogAndClose(user, "Bulk Edit English Proficiency");

    await openTab(user, "Academic Requirements");
    await user.click((await screen.findAllByTitle("Edit"))[0]);
    await expectDialogAndClose(user, "Edit Academic Requirement");
    await user.click((await screen.findAllByTitle("Delete"))[0]);
    await expectDialogAndClose(user, "Delete Academic Requirement");
    await user.click(screen.getByRole("button", { name: "Bulk Add Academic" }));
    await expectDialogAndClose(user, "Bulk Edit Academic Requirements");

    await openTab(user, "Scholarships");
    await user.click(await screen.findByTitle("Add / edit scholarship"));
    await expectDialogAndClose(user, /Edit Scholarship/);
    await user.click(await screen.findByTitle("Delete"));
    await expectDialogAndClose(user, "Delete Scholarship");
    await user.click(screen.getByRole("button", { name: "Bulk Add Scholarship" }));
    await expectDialogAndClose(user, "Bulk Add Scholarship");

    await openTab(user, "Locations");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog", { name: "Edit Location" });
    expectAccessibleDialog();
  });

  it("opens a Raw Data confirmation through its visible row action", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(user, "Raw Data");
    await screen.findByText("Staged Accessible Course");

    await user.click(await screen.findByTitle("Edit"));
    await expectDialogAndClose(user, /Edit Course/);

    await user.click(await screen.findByTitle("Map from Backup"));
    await expectDialogAndClose(user, "Map from Backup");

    await user.click(await screen.findByTitle("Force Approve (bypass confidence gate)"));
    await expectDialogAndClose(user, "Force Approve Course");

    await user.click(await screen.findByTitle("Delete"));
    await expectDialogAndClose(user, "Delete staged course?");

    await user.click(screen.getByRole("button", { name: "Remove All" }));
    await expectDialogAndClose(user, "Remove all staged data?");

    await user.click(screen.getByRole("button", { name: "Import All (1)" }));
    await expectDialogAndClose(user, "Import all pending courses?");

    await user.click(screen.getByTitle("Select all"));
    await user.click(screen.getByRole("button", { name: "Force Approve (1)" }));
    await expectDialogAndClose(user, "Force Approve 1 Course");
    await user.click(screen.getByRole("button", { name: "Reject (1)" }));
    await expectDialogAndClose(user, "Reject 1 Course With Reason");
    await user.click(screen.getByRole("button", { name: "Delete (1)" }));
    await screen.findByRole("dialog", { name: "Delete 1 staged row?" });
    expectAccessibleDialog();
  });
});