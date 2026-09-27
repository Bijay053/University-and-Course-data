// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { CourseListResponse } from "@workspace/api-client-react";
import { CourseCampusFees } from "./course-campus-fees";
import Courses from "@/pages/courses";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("course list campus tuition contract", () => {
  it("renders exact campus tuition from the data envelope without a scalar fee", () => {
    // Same camelCase offering contract returned by both Python course list routes.
    const response: CourseListResponse = { total: 1, page: 1, limit: 50, data: [{
      id: 201, universityId: 7, name: "Canonical award", status: "active",
      createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-01T00:00:00Z",
      internationalFee: null, offerings: [
        { id: "1", location: "London", feeAmount: 19050, feeCurrency: "GBP", feeTerm: "Full Course", feeYear: 2026 },
        { id: "2", location: "Paris", feeAmount: 12000, feeCurrency: "EUR", feeTerm: "Annual", feeYear: 2027 },
        { id: "3", location: "Online", feeAmount: null, feeCurrency: null, feeTerm: null, feeYear: null },
      ],
    }] };
    const { rerender } = render(<CourseCampusFees course={response.data[0]} />);
    expect(screen.getByTestId("text-campus-fee-1").textContent).toBe("London: GBP 19,050 / Full Course · 2026");
    expect(screen.getByTestId("text-campus-fee-2").textContent).toBe("Paris: EUR 12,000 / Annual · 2027");
    expect(screen.getByText("Fee not published for this location")).toBeTruthy();
    rerender(<CourseCampusFees course={{ ...response.data[0], internationalFee: 99999 }} />);
    expect(screen.queryByText(/99,999/)).toBeNull();
  });

  it("preserves zero fees and never guesses missing units", () => {
    render(<CourseCampusFees course={{ offerings: [{ id: "1", location: "Online", feeAmount: 0 }] }} />);
    expect(screen.getByText("Currency not published 0 / Term not published · Year not published")).toBeTruthy();
  });

  it("retains legacy scalar tuition only without offerings", () => {
    render(<CourseCampusFees course={{ internationalFee: 10000, currency: "AUD", feeTerm: "Annual", feeYear: 2025 }} />);
    expect(screen.getByText("AUD 10,000 / Annual · 2025")).toBeTruthy();
  });

  it("renders the actual Courses page through the generated API hook and HTTP envelope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: [{
        id: 201, universityId: 7, name: "Canonical award", internationalFee: null,
        offerings: [
          { id: "1", location: "London", feeAmount: 19050, feeCurrency: "GBP", feeTerm: "Full Course", feeYear: 2026 },
          { id: "2", location: "Paris", feeAmount: 12000, feeCurrency: "EUR", feeTerm: "Annual", feeYear: 2027 },
        ],
      }], total: 1, page: 1, limit: 20,
    }), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><Courses /></QueryClientProvider>);
    const campuses = await screen.findAllByTestId("text-campus-fee-1");
    expect(campuses[0].textContent).toBe("London: GBP 19,050 / Full Course · 2026");
    expect(screen.getAllByTestId("text-campus-fee-2")[0].textContent).toBe("Paris: EUR 12,000 / Annual · 2027");
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/courses?");
    client.clear();
  });
});