// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CourseCard } from "./course-card";
import type { CourseResult } from "./lib";

afterEach(cleanup);

const longName = "2D Animation and Stop Motion (with Foundation Year and Optional Placement Year) BA (Hons)";
const base: CourseResult = {
  id: 41, course_name: longName,
  university: { id: 4, name: "Example University", city: "London", country: "United Kingdom", logo_url: null },
  course_location: "London", degree_level: "Graduate Certificate & Diploma", category: null,
  duration: 4, duration_term: "Years", intakes: ["September"],
  international_fee: 17000, currency: "GBP", fee_term: "Annual",
  english_requirements: { ielts_overall: null, pte_overall: null, toefl_overall: null, cae_overall: null, duolingo_overall: null },
  course_url: "https://example.edu/course",
};

describe("CourseCard title and shortlist clearance", () => {
  it.each([
    ["grid", true, "sm:pr-9"],
    ["grid", false, "sm:pr-9"],
    ["list", true, "sm:pr-0"],
    ["list", false, "sm:pr-9"],
  ] as const)("reserves control space in %s view with fees=%s", (view, hasFee, desktopClearance) => {
    render(<CourseCard course={{ ...base, international_fee: hasFee ? 17000 : null }}
      view={view} selected={false} shortlisted={false} onSelect={vi.fn()} onToggleShortlist={vi.fn()} index={0} />);
    const title = screen.getByRole("heading", { name: longName });
    expect(title.classList.contains("pr-11")).toBe(true);
    expect(title.classList.contains(desktopClearance)).toBe(true);
    const select = screen.getByRole("button", { name: longName });
    expect(select.classList.contains("whitespace-normal")).toBe(true);
    expect(select.classList.contains("max-w-full")).toBe(true);
    expect(title.textContent).toBe(longName);
  });

  it.each(["grid", "list"] as const)("keeps full unbroken titles readable without a degree or fee in %s view", (view) => {
    const name = "InternationalMultidisciplinaryProfessionalPractice".repeat(4);
    render(<CourseCard course={{ ...base, course_name: name, degree_level: null, international_fee: null }}
      view={view} selected={false} shortlisted={false} onSelect={vi.fn()} onToggleShortlist={vi.fn()} index={0} />);
    const select = screen.getByRole("button", { name });
    expect(select.textContent).toBe(name);
    expect(select.classList.contains("[overflow-wrap:anywhere]")).toBe(true);
    expect(select.className).not.toMatch(/truncate|line-clamp/);
  });

  it("keeps course selection and shortlist toggling independent", () => {
    const onSelect = vi.fn();
    const onToggleShortlist = vi.fn();
    const props = { course: base, view: "grid" as const, selected: false, shortlisted: false, onSelect, onToggleShortlist, index: 0 };
    const { rerender } = render(<CourseCard {...props} />);
    fireEvent.click(screen.getByRole("button", { name: `Add ${longName} to shortlist` }));
    expect(onToggleShortlist).toHaveBeenCalledOnce();
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: longName }));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onToggleShortlist).toHaveBeenCalledOnce();
    rerender(<CourseCard {...props} selected shortlisted />);
    expect(screen.getByRole("button", { name: longName }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: `Remove ${longName} from shortlist` }).getAttribute("aria-pressed")).toBe("true");
  });
});
