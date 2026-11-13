// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AssistantRepairPreview } from "./assistant-repair-preview";

afterEach(cleanup);

describe("delivery repair preview", () => {
  it("shows current and proposed modes without treating them as numeric IELTS scores", () => {
    const course = { id: 59824, name: "Addiction and Mental Health MSc",
      url: "https://www.aston.ac.uk/study/courses/addiction-and-mental-health-msc/september-2027" };
    render(<AssistantRepairPreview testId="delivery-preview" scope={[course]} previews={[{
      ...course, status: "supported", reason: "",
      changes: [{ field: "study_mode", oldValue: "Online", newValue: "On Campus" }],
      source: { url: course.url, quote: "Location: Aston University, Birmingham" },
    }]} />);
    expect(screen.getByText("Study mode")).toBeTruthy();
    expect(screen.getByText("Online")).toBeTruthy();
    expect(screen.getByText("On Campus")).toBeTruthy();
    expect(screen.getByText("Location: Aston University, Birmingham")).toBeTruthy();
  });
  it("labels a read-back correction as saved rather than still awaiting execution", () => {
    const course = { id: 1, name: "Example MSc", url: "https://www.example.edu/course" };
    render(<AssistantRepairPreview testId="saved-delivery" scope={[course]} applied previews={[{
      ...course, status: "supported", reason: "",
      changes: [{ field: "study_mode", oldValue: "Online", newValue: "On Campus" }],
    }]} />);
    expect(screen.getByText("Saved")).toBeTruthy();
    expect(screen.getByText("Updated")).toBeTruthy();
    expect(screen.queryByText("Proposed")).toBeNull();
    expect(screen.getByText("These changes were saved and read back. No courses were published.")).toBeTruthy();
  });
});
