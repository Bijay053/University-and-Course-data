// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PublishedFeeVariants, feeVariantSummary, feeVariantNeedsReview } from "./published-fee-variants";
import { ReviewScrapedCoursesTable, type ReviewStagedCourse } from "./review-scraped-courses-table";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const source = "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/";
const option = (amount: number, campus: string, year = 2026, period = "Full Course") => ({
  amount, currency: "GBP", campus, year, period, study_variant: "Standard",
  source_url: source, snippet: `International Students | ${year} | ${campus}: £${amount} <script>alert("x")</script>`,
});
const authority = (selected = [option(17500, "Outside London"), option(19050, "London")]) => ({
  status: "range", international_fee: null, fee_year: 2026, fee_term: "Full Course",
  selected, options: [...selected, option(21000, "London", 2027)],
});
const carrier = { extractionMethod: { fee_variants: authority() } };
const selection = (selectedOptionId: string | null = null) => ({
  snapshotToken: "snapshot-2026",
  selectedOptionId,
  options: [
    { optionId: "london-annual", amount: 17500, currency: "GBP", campus: "London", studyVariant: "Standard", year: 2026, period: "Annual", sourceUrl: source, snippet: "2026 annual" },
    { optionId: "london-full", amount: 17500, currency: "GBP", campus: "London", studyVariant: "Standard", year: 2026, period: "Full Course", sourceUrl: source, snippet: "2026 full course" },
    { optionId: "london-2027", amount: 17500, currency: "GBP", campus: "London", studyVariant: "Standard", year: 2027, period: "Annual", sourceUrl: source, snippet: "2027 annual" },
  ],
});
const course = (extra: object = {}) => ({
  id: 41, courseName: "MSc Healthcare Management", internationalFee: null,
  ...carrier, ...extra,
} as ReviewStagedCourse);

describe("persisted published fee alternatives", () => {
  it("renders the actual range, period, year and source options in the existing staged table", () => {
    render(<ReviewScrapedCoursesTable courses={[course()]} readOnly />);
    expect(screen.getByTestId("fee-summary-41").textContent).toBe("£17,500–£19,050 GBP · 2026 · Full Course");
    expect(screen.queryByTitle("Missing international fee")).toBeNull();
    expect(screen.getByTestId("fee-review-41").textContent).toContain("unverified fees stay pending");
    fireEvent.click(screen.getByTestId("expand-fees-41"));
    const link = screen.getByTestId("fee-source-41-selected-0");
    expect(link.getAttribute("href")).toBe(source);
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    const evidence = screen.getByTestId("fee-option-41-selected-0");
    expect(evidence.textContent).toContain("Outside London");
    expect(evidence.textContent).toContain('<script>alert("x")</script>');
    expect(evidence.querySelector("script")).toBeNull();
    expect(screen.getByTestId("fee-summary-41").textContent).not.toContain("21,000");
    expect(feeVariantNeedsReview(carrier)).toBe(true);
  });

  it.each([
    [17200, 18100, 2026, "Annual", "£17,200–£18,100 GBP · 2026 · Annual (per year)"],
    [18250, 19600, 2026, "Full Course", "£18,250–£19,600 GBP · 2026 · Full Course"],
    [17000, 18500, 2025, "Full Course", "£17,000–£18,500 GBP · 2025 · Full Course"],
  ])("preserves published campus/year/period metadata (%s–%s)", (min, max, year, period, expected) => {
    const fees = authority([option(min as number, "Outside London", year as number, period as string), option(max as number, "London", year as number, period as string)]);
    expect(feeVariantSummary({ extraction_method: { fee_variants: fees } })).toBe(expected);
  });

  it("keeps different years and annual/full-course amounts separate", () => {
    const fees = authority([option(17200, "London", 2026, "Annual"), option(19050, "London", 2027, "Full Course")]);
    expect(feeVariantSummary({ feeVariants: fees })).toBe("£17,200 GBP · 2026 · Annual (per year); £19,050 GBP · 2027 · Full Course");
  });

  it("does not ask for a fee choice for campus prices in one fee cohort", () => {
    const pending = course({ feeSelection: selection() });
    render(<PublishedFeeVariants course={pending} id={41} />);
    expect(screen.getByTestId("fee-review-41").textContent).toContain("Review the published options for each location");
    expect(screen.queryByText("Choose a published fee before approval")).toBeNull();
    expect(screen.queryByTestId("fee-selection-41")).toBeNull();
    fireEvent.click(screen.getByTestId("expand-fees-41"));
    expect(screen.getByTestId("fee-option-41-selected-0").textContent).toContain("Outside London");
    expect(screen.getByTestId("fee-option-41-selected-1").textContent).toContain("London");
    expect(feeVariantNeedsReview(pending)).toBe(true);
  });

  it("shows uncertain cohorts, routes and campus evidence without prompting an arbitrary fee choice", () => {
    const mixed = authority([option(17500, "London", 2026), option(19050, "London", 2027)]);
    const { rerender } = render(<PublishedFeeVariants course={course({ feeVariants: mixed, feeSelection: selection() })} id={41} />);
    expect(screen.getByTestId("fee-review-41").textContent).toContain("automatic approval stays pending");
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(screen.queryByText("Choose a published fee before approval")).toBeNull();
    expect(feeVariantNeedsReview(course({ feeVariants: mixed, feeSelection: selection() }))).toBe(true);
    const routes = authority([option(17500, "London"), { ...option(19050, "Outside London"), study_variant: "Professional Practice" }]);
    rerender(<PublishedFeeVariants course={course({ feeVariants: routes, feeSelection: selection() })} id={41} />);
    expect(screen.getByTestId("fee-review-41").textContent).toContain("automatic approval stays pending");
    expect(screen.queryByRole("radiogroup")).toBeNull();
    const conflicting = authority([option(17500, "London"), option(19050, "London"), option(17000, "Outside London")]);
    rerender(<PublishedFeeVariants course={course({ feeVariants: conflicting, feeSelection: selection() })} id={41} />);
    expect(screen.getByTestId("fee-review-41").textContent).toContain("automatic approval stays pending");
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });

  it("renders uniform MBA authority without substituting a domestic fee", () => {
    const fees = { ...authority([option(20600, "All campuses")]), status: "uniform" };
    render(<PublishedFeeVariants course={{ feeVariants: fees }} id="mba" />);
    expect(screen.getByTestId("fee-summary-mba").textContent).toBe("£20,600 GBP · 2026 · Full Course");
    expect(screen.getByTestId("fee-assigned-mba").textContent).toContain("All campuses · Standard · 2026 · Full Course");
    expect(feeVariantNeedsReview({ feeVariants: fees })).toBe(false);
    expect(screen.queryByText(/16,900/)).toBeNull();
  });

  it("shows the course-owned campus for a scoped uniform price, not the broad fee-schedule label", () => {
    const fees = { ...authority([option(17500, "Outside London")]), status: "uniform" };
    const scoped = course({
      extractionMethod: { fee_variants: fees, campus_fee_scope: { locations: ["Birmingham", "Leeds", "Manchester"] } },
      feeSelection: selection(),
    });
    render(<PublishedFeeVariants course={scoped} id={41} />);
    expect(screen.getByTestId("fee-summary-41").textContent).toContain("£17,500");
    expect(screen.getByTestId("fee-assigned-41").textContent).toContain("Birmingham, Leeds, Manchester · Standard · 2026");
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(screen.queryByText("Choose a published fee before approval")).toBeNull();
    expect(feeVariantNeedsReview(scoped)).toBe(false);
  });

  it("refreshes from persisted metadata rather than retaining an old range", () => {
    const { rerender } = render(<ReviewScrapedCoursesTable courses={[course()]} readOnly />);
    const fees = authority([option(17000, "Outside London", 2025), option(18500, "London", 2025)]);
    rerender(<ReviewScrapedCoursesTable courses={[course({ extractionMethod: null, extraction_method: { fee_variants: fees } })]} readOnly />);
    expect(screen.getByTestId("fee-summary-41").textContent).toContain("£17,000–£18,500 GBP · 2025");
  });

  it("shows unresolved options as review required, not an invented price or missing scalar", () => {
    const fees = { ...authority(), status: "unresolved", selected: [] };
    render(<ReviewScrapedCoursesTable courses={[course({ feeVariants: fees })]} readOnly />);
    expect(screen.getByTestId("fee-summary-41").textContent).toBe("Published fee unresolved");
    expect(feeVariantNeedsReview({ feeVariants: fees })).toBe(true);
    expect(screen.queryByTitle("Missing international fee")).toBeNull();
  });

  it("retains legacy scalar and missing fee rendering", () => {
    const { rerender } = render(<ReviewScrapedCoursesTable courses={[course({ extractionMethod: "legacy", internationalFee: 12000, currency: "GBP", feeTerm: "Annual" })]} readOnly />);
    expect(screen.getByText("£12,000")).toBeTruthy();
    expect(screen.queryByTestId("published-fees-41")).toBeNull();
    rerender(<ReviewScrapedCoursesTable courses={[course({ extractionMethod: null })]} readOnly />);
    expect(screen.getByTitle("Missing international fee")).toBeTruthy();
  });

  it("rejects malformed selected amounts and unsafe source links", () => {
    const fees = authority([{ ...option(17500, "London"), source_url: "javascript:alert(1)" }]);
    render(<PublishedFeeVariants course={{ feeVariants: fees }} id="unsafe" />);
    expect(screen.queryByTestId("fee-source-unsafe-selected-0")).toBeNull();
    expect(feeVariantSummary({ feeVariants: { ...fees, selected: [{ amount: "17500" }] } })).toBeNull();
  });

  it("retains fail-closed fee-selection checks without offering a manual choice", () => {
    const mixed = authority([option(17500, "London", 2026), option(17500, "London", 2027)]);
    const pending = course({ feeVariants: mixed, feeSelection: selection() });
    expect(feeVariantNeedsReview(pending)).toBe(true);
    const saved = course({ feeVariants: mixed, feeSelection: selection("london-full") });
    render(<PublishedFeeVariants course={pending} id={41} />);
    expect(screen.getByTestId("fee-review-41").textContent).toContain("automatic approval stays pending");
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(screen.queryByTestId("save-fee-choice-41")).toBeNull();
    expect(feeVariantNeedsReview(saved)).toBe(false);
  });

  it("keeps historic review readonly, and leaves uniform fees eligible without a choice", () => {
    render(<ReviewScrapedCoursesTable courses={[course({ feeSelection: selection() })]} readOnly />);
    expect(screen.queryByTestId("save-fee-choice-41")).toBeNull();
    expect(feeVariantNeedsReview({ feeVariants: { ...authority([option(20600, "All campuses")]), status: "uniform" } })).toBe(false);
  });
});