// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ComparePage from "./compare";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });

it("compares exact campus prices and Full Course periods through the HTTP response contract", async () => {
  window.history.replaceState({}, "", "/compare?ids=201,202");
  const course = {
    id: 201, course_name: "Canonical award", university: { id: 7, name: "University" },
    intakes: [], english_requirements: [], academic_requirements: [],
    international_fee: null, international_fee_yearly: null, currency: null, fee_term: null,
    offerings: [
      { id: "1", location: "London", feeAmount: 19050, feeCurrency: "GBP", feeTerm: "Full Course", feeYear: 2026 },
      { id: "2", location: "Leeds", feeAmount: 17500, feeCurrency: "GBP", feeTerm: "Full Course", feeYear: 2026 },
      { id: "3", location: "Paris", feeAmount: 12000, feeCurrency: "EUR", feeTerm: "Semester", feeYear: 2027 },
    ],
  };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ courses: [
    course, { ...course, id: 202, course_name: "Legacy award", offerings: [],
      international_fee: 95000, fee_term: "Full Course", currency: "AUD", fee_year: 2025 },
  ] }), { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  render(<ComparePage />);
  expect((await screen.findByTestId("text-campus-fee-1")).textContent).toBe("London: GBP 19,050 / Full Course · 2026");
  expect(screen.getByTestId("text-campus-fee-2").textContent).toBe("Leeds: GBP 17,500 / Full Course · 2026");
  expect(screen.getByTestId("text-campus-fee-3").textContent).toBe("Paris: EUR 12,000 / Semester · 2027");
  expect(screen.getByText("AUD 95,000 / Full Course · 2025")).toBeTruthy();
  expect(screen.queryByText(/\/ Year/)).toBeNull();
  expect(fetchMock.mock.calls[0][0]).toContain("/api/search/compare?ids=201%2C202");
});