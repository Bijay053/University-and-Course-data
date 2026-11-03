// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { IntakeSelect } from "./intake-select";

afterEach(cleanup);
function Harness() {
  const [value, onChange] = useState<string[]>([]);
  return <IntakeSelect options={["January", "February", "September"]} value={value} onChange={onChange} />;
}
describe("intake multi-select dropdown", () => {
  it("keeps multiple selections while searching, deselecting and clearing", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("select-intakes"));
    fireEvent.click(screen.getByRole("checkbox", { name: "January" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "February" }));
    expect(screen.getByTestId("select-intakes").textContent).toContain("January, February");
    fireEvent.change(screen.getByRole("textbox", { name: "Search intake months" }), { target: { value: "SEPT" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "September" }));
    expect(screen.getByText("3 selected")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    fireEvent.click(screen.getByTestId("select-intakes"));
    expect((screen.getByRole("checkbox", { name: "January" }) as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: "February" }));
    expect(screen.getByText("2 selected")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear intakes" }));
    expect(screen.getByTestId("select-intakes").textContent).toContain("Any intake");
  });
  it("shows no matches and closes with Escape without propagating", () => {
    render(<Harness />);
    const trigger = screen.getByTestId("select-intakes");
    fireEvent.click(trigger);
    const search = screen.getByRole("textbox", { name: "Search intake months" });
    fireEvent.change(search, { target: { value: "missing" } });
    expect(screen.getByText("No intakes found.")).toBeTruthy();
    fireEvent.keyDown(search, { key: "Escape" });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger);
  });
});
