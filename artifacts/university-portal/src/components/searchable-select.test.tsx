// @vitest-environment jsdom

import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SearchableSelect } from "./searchable-select";

afterEach(cleanup);

function Harness() {
  const [value, setValue] = useState("");
  return (
    <SearchableSelect
      value={value}
      onChange={setValue}
      options={[
        "Bachelor Degree",
        "Master of Technology Management",
        "Undergraduate",
      ]}
      placeholder="Any qualification"
      searchPlaceholder="Search qualifications…"
    />
  );
}

describe("SearchableSelect", () => {
  it("filters options and selects a matching qualification", () => {
    render(<Harness />);

    fireEvent.click(
      screen.getByRole("combobox", { name: "Any qualification" }),
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Search qualifications…" }),
      { target: { value: "technology" } },
    );

    expect(
      screen.queryByRole("option", { name: "Bachelor Degree" }),
    ).toBeNull();

    fireEvent.click(
      screen.getByRole("option", {
        name: "Master of Technology Management",
      }),
    );

    expect(screen.getByRole("combobox", {
      name: "Any qualification",
    }).textContent).toContain("Master of Technology Management");
  });

  it("shows a clear empty state when no option matches", () => {
    render(<Harness />);

    fireEvent.click(
      screen.getByRole("combobox", { name: "Any qualification" }),
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Search qualifications…" }),
      { target: { value: "not a real qualification" } },
    );

    expect(screen.getByText("No matching qualifications")).toBeTruthy();
  });
});