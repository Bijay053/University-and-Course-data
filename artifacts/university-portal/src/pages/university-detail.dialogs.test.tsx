// @vitest-environment jsdom

import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
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

function renderPage() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/scholarship-courses")
      ? [{ id: 42, name: "Accessible Course", degreeLevel: "Bachelor", category: "Business", scholarships: [{ id: 8, name: "Merit Award", details: "For strong applicants", eligibilityCriteria: "International students", amount: 5000, percentage: null, currency: "AUD" }] }]
      : url.includes("/academic-requirements")
      ? [{ id: 9, courseId: 42, courseName: "Accessible Course", degreeLevel: "Bachelor", academicLevel: "Year 12", academicScore: 75, scoreType: "%", academicCountry: "Australia" }]
      : url.includes("/assessment-notes")
      ? [{ id: 10, country: "Australia", raw_text: "Representative assessment note", parsed_data: null, created_at: "2026-09-03T00:00:00Z" }]
      : url.includes("/locations")
      ? [{ id: 11, universityId: 7, displayName: "City Campus", fullAddress: "1 Campus Way", city: "Sydney", stateRegion: "NSW", country: "Australia", latitude: -33.86, longitude: 151.2, courseCount: 1, isVerified: true }]
      : url.includes("/scrape/staged")
      ? [{ id: 12, course_name: "Staged Accessible Course", status: "pending", completeness: 45 }]
      : url.includes("/repair/missing/")
      ? { courses: [] }
      : url.includes("/change-detection/")
        ? { summary: { total: 0 }, events: [] }
        : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <UniversityDetail />
    </QueryClientProvider>,
  );
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