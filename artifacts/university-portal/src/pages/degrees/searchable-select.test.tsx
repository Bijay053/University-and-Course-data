// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SearchableSelect } from "./searchable-select";

const opts = [{ value: "AU", label: "Australia" }, { value: "AT", label: "Austria" }, { value: "CA", label: "Canada" }];
function Harness({ onChange }: { onChange: (v: string) => void }) {
  const [v, setV] = useState("");
  return <SearchableSelect label="Country" testId="dd" anyLabel="Any country" value={v} options={opts} onChange={(x) => { setV(x); onChange(x); }} />;
}
afterEach(cleanup);

describe("SearchableSelect", () => {
  it("filters case-insensitively, shows no results and selects exact values", () => {
    const spy = vi.fn();
    render(<Harness onChange={spy} />);
    fireEvent.click(screen.getByTestId("dd"));
    fireEvent.change(screen.getByTestId("dd-filter"), { target: { value: "AUST" } });
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Australia", "Austria"]);
    fireEvent.change(screen.getByTestId("dd-filter"), { target: { value: "zz" } });
    expect(screen.getByText("No matches")).toBeTruthy();
    fireEvent.change(screen.getByTestId("dd-filter"), { target: { value: "can" } });
    fireEvent.click(screen.getByRole("option", { name: "Canada" }));
    expect(spy).toHaveBeenLastCalledWith("CA");
    expect(screen.getByTestId("dd").textContent).toBe("Canada");
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.click(screen.getByTestId("dd"));
    expect(screen.getByRole("option", { name: "Canada" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("option", { name: "Any country" }));
    expect(spy).toHaveBeenLastCalledWith("");
    expect(screen.getByTestId("dd").textContent).toBe("Any country");
  });

  it("supports arrow keys, Enter and Escape", () => {
    const spy = vi.fn();
    render(<Harness onChange={spy} />);
    fireEvent.keyDown(screen.getByTestId("dd"), { key: "ArrowDown" });
    const input = screen.getByTestId("dd-filter");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(spy).toHaveBeenLastCalledWith("AT");
    fireEvent.click(screen.getByTestId("dd"));
    fireEvent.keyDown(screen.getByTestId("dd-filter"), { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(screen.getByTestId("dd"));
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
