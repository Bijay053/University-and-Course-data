// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AssistantRepairPreview, type RepairPreview } from "./assistant-repair-preview";

afterEach(cleanup);
const course = { id: 1, name: "Engineering Management MSc",
  url: "https://www.aston.ac.uk/study/courses/engineering-management-msc" };
const preview = (oldValue: string[], newValue: string[]): RepairPreview => ({
  ...course, status: "supported", reason: "",
  changes: [{ field: "intake_months", oldValue, newValue }],
  source: { url: course.url, quote: "Start dates: January, April, September 2027" },
});

describe("intake correction comparisons", () => {
  it("shows all current and proposed months with readable separators", () => {
    render(<AssistantRepairPreview scope={[course]}
      previews={[preview(["January"], ["January", "April", "September"])]} testId="intake-preview" />);
    expect(screen.getByRole("rowheader", { name: "Intakes" })).toBeTruthy();
    expect(screen.getByRole("cell", { name: "January" })).toBeTruthy();
    expect(screen.getByRole("cell", { name: "January, April, September" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Source evidence" }).getAttribute("href")).toBe(course.url);
    expect(screen.getByText(/These are proposals, not applied changes/)).toBeTruthy();
  });
  it("does not show a change for equal month arrays with different references", () => {
    render(<AssistantRepairPreview scope={[course]}
      previews={[preview(["September"], ["September"])]} testId="intake-preview" />);
    expect(screen.getByRole("cell", { name: "Unchanged" })).toBeTruthy();
  });
  it("shows missing intake values without a blank cell", () => {
    render(<AssistantRepairPreview scope={[course]}
      previews={[preview([], ["September"])]} testId="intake-preview" />);
    expect(screen.getByRole("cell", { name: "Missing" })).toBeTruthy();
  });
});
