// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ResultLocationFee } from "./search";

afterEach(cleanup);

type SearchResult = React.ComponentProps<typeof ResultLocationFee>["result"];
const result: SearchResult = {
  id: 17, course_name: "Master of Health", university: {
    id: 3, name: "Example University", logo_url: null, city: "London", country: "UK", website: null,
  },
  course_location: "London, Leeds", degree_level: null, category: null, sub_category: null,
  duration: null, duration_term: null, duration_years: null, intakes: [],
  international_fee: 19050, international_fee_yearly: 19050, currency: "GBP",
  fee_term: "Annual", application_fee: null, course_url: null,
  english_requirements: {
    ielts_overall: null, pte_overall: null, pte_listening: null, pte_writing: null,
    toefl_overall: null, cae_overall: null, duolingo_overall: null,
  },
};

describe("search course location offerings", () => {
  it("offers every location under one course and shows only the selected location's fee", () => {
    const offerings = [
      { id: "london", location: "London", feeAmount: 19050, feeCurrency: "GBP", feeTerm: "Full Course", feeYear: 2026 },
      { id: "leeds", location: "Leeds", feeAmount: 17500, feeCurrency: "GBP", feeTerm: "Full Course", feeYear: 2026 },
      { id: "online", location: "Online", feeAmount: null, feeCurrency: null, feeTerm: null, feeYear: null },
    ];
    render(<ResultLocationFee result={{ ...result, offerings }} locationFilter="" />);
    const select = screen.getByRole("combobox", { name: "Location" });
    expect(screen.getAllByRole("option")).toHaveLength(4);
    expect(screen.getByText("Select a location to see its fee")).toBeTruthy();
    expect(screen.queryByText(/19,050/)).toBeNull();
    fireEvent.change(select, { target: { value: "leeds" } });
    expect(screen.getByText("GBP 17,500 / Full Course · 2026")).toBeTruthy();
    fireEvent.change(select, { target: { value: "london" } });
    expect(screen.getByText("GBP 19,050 / Full Course · 2026")).toBeTruthy();
    fireEvent.change(select, { target: { value: "online" } });
    expect(screen.getByText("Fee not published for this location")).toBeTruthy();
  });

  it("falls back to legacy location and scalar fee only when offerings are absent", () => {
    render(<ResultLocationFee result={result} locationFilter="" />);
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getByText("London, Leeds")).toBeTruthy();
    expect(screen.getByText("GBP 19,050 / Year")).toBeTruthy();
  });

  it("does not use a legacy scalar fee for an offering without a published fee", () => {
    render(<ResultLocationFee result={{ ...result, offerings: [
      { id: "leeds", location: "Leeds", feeAmount: null, feeCurrency: null, feeTerm: null, feeYear: null },
    ] }} locationFilter="" />);
    expect(screen.getByText("Fee not published for this location")).toBeTruthy();
    expect(screen.queryByText(/19,050/)).toBeNull();
  });

  it("uses a uniquely matching search location fee, allows switching, and resets on filter change", () => {
    const offerings = [
      { id: "london", location: "London", feeAmount: 19050, feeCurrency: "GBP", feeTerm: "Annual", feeYear: 2026 },
      { id: "leeds", location: "Leeds", feeAmount: 17500, feeCurrency: "GBP", feeTerm: "Annual", feeYear: 2026 },
    ];
    const { rerender } = render(<ResultLocationFee result={{ ...result, offerings }} locationFilter="lONdon" />);
    const select = screen.getByRole("combobox", { name: "Location" }) as HTMLSelectElement;
    expect(select.value).toBe("london");
    expect(screen.getByText("GBP 19,050 / Annual · 2026")).toBeTruthy();
    fireEvent.change(select, { target: { value: "leeds" } });
    expect(screen.getByText("GBP 17,500 / Annual · 2026")).toBeTruthy();
    rerender(<ResultLocationFee result={{ ...result, offerings: [...offerings] }} locationFilter="lONdon" />);
    expect(select.value).toBe("leeds"); // Refetch does not erase an explicit choice.
    rerender(<ResultLocationFee result={{ ...result, offerings }} locationFilter="Leeds" />);
    expect(select.value).toBe("leeds");
    rerender(<ResultLocationFee result={{ ...result, offerings }} locationFilter="" />);
    expect(select.value).toBe("");
    expect(screen.getByText("Select a location to see its fee")).toBeTruthy();
    rerender(<ResultLocationFee result={{ ...result, offerings }} locationFilter="London" />);
    expect(select.value).toBe("london");
  });

  it("does not guess a fee when a location substring matches multiple offerings", () => {
    const offerings = [
      { id: "london", location: "London", feeAmount: 19050, feeCurrency: "GBP", feeTerm: "Annual", feeYear: 2026 },
      { id: "east", location: "East London", feeAmount: 17500, feeCurrency: "GBP", feeTerm: "Annual", feeYear: 2026 },
    ];
    const { rerender } = render(<ResultLocationFee result={{ ...result, offerings }} locationFilter="lon" />);
    expect((screen.getByRole("combobox", { name: "Location" }) as HTMLSelectElement).value).toBe("");
    expect(screen.getByText("Select a location to see its fee")).toBeTruthy();
    rerender(<ResultLocationFee result={{ ...result, offerings }} locationFilter="London" />);
    expect(screen.getByText("GBP 19,050 / Annual · 2026")).toBeTruthy();
  });
});