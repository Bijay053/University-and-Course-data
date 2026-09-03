// @vitest-environment jsdom

import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, it, vi } from "vitest";

import UniversityDetail from "./university-detail";

vi.mock("@workspace/api-client-react", () => ({
  getGetUniversityQueryKey: (id: number) => ["university", id],
  getListCoursesQueryKey: () => ["courses"],
  useGetUniversity: () => ({
    data: { id: 7, name: "Accessible University", city: "Sydney", country: "Australia", website: "https://example.edu" },
    isLoading: false,
  }),
  useListCourses: () => ({
    data: { data: [{ id: 42, name: "Accessible Course" }], total: 1 },
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
  useToast: () => ({ toast: vi.fn() }),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/scholarship-courses")
      || url.includes("/academic-requirements")
      || url.includes("/assessment-notes")
      || url.includes("/locations")
      ? []
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
});