// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CompareView } from "./compare-view";
import type { CompareCourse } from "./lib";

const pdf = vi.hoisted(() => ({ create: vi.fn(), save: vi.fn() }));
vi.mock("./compare-pdf", () => ({ createComparisonPdf: pdf.create }));
const course: CompareCourse = {
  id: 7, course_name: "Master of Māori Studies",
  university: { id: 9, name: "Harbour University", city: "Sydney", country: "Australia", logo_url: null },
  course_location: "Sydney", degree_level: "Master", category: null, duration: 2, duration_term: "Year",
  intakes: ["February"], international_fee: 28000, currency: "AUD", fee_term: "Annual", fee_year: 2026,
  course_url: "https://example.edu/course", study_mode: "On Campus", english_requirements: [], academic_requirements: [],
};
beforeEach(() => {
  pdf.create.mockReset().mockResolvedValue({ save: pdf.save });
  pdf.save.mockReset().mockResolvedValue(undefined);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ courses: [course] }) }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("comparison PDF download", () => {
  it("waits for loaded courses and downloads the displayed comparison once", async () => {
    render(<CompareView ids={[7, 8]} onClose={() => {}} />);
    const button = screen.getByTestId("button-download-compare") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    await screen.findByText("Master of Māori Studies");
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(pdf.save).toHaveBeenCalledTimes(1));
    expect(pdf.create).toHaveBeenCalledWith([course], 2, expect.any(Date));
    expect(pdf.save).toHaveBeenCalledWith(expect.stringMatching(/^study-info-centre-course-comparison-\d{4}-\d{2}-\d{2}\.pdf$/), { returnPromise: true });
  });

  it("shows download errors and lets the user retry", async () => {
    pdf.create.mockRejectedValueOnce(new Error("Font unavailable"));
    render(<CompareView ids={[7]} onClose={() => {}} />);
    await screen.findByText("Master of Māori Studies");
    fireEvent.click(screen.getByTestId("button-download-compare"));
    expect((await screen.findByTestId("error-download-compare")).textContent).toContain("Please try again");
    fireEvent.click(screen.getByTestId("button-download-compare"));
    await waitFor(() => expect(pdf.save).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("error-download-compare")).toBeNull();
  });

  it("does not export empty or unavailable comparisons", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ courses: [] }) }));
    render(<CompareView ids={[7]} onClose={() => {}} />);
    await screen.findByText("These courses are no longer listed.");
    expect((screen.getByTestId("button-download-compare") as HTMLButtonElement).disabled).toBe(true);
    expect(pdf.create).not.toHaveBeenCalled();
  });
});
