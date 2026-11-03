// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { UniversityInsights } from "./university-insights";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows empty guidance, retries errors, and hides old university data", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce({ ok: false })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ insights: [] }) })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ insights: [{ id: 1, country: "Nepal", text: "<script>unsafe()</script>", updated_at: null }] }) });
  vi.stubGlobal("fetch", fetcher);
  const view = render(<UniversityInsights universityId={1} universityName="First" />);
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  await waitFor(() => expect(view.container.textContent).toBe(""));
  view.rerender(<UniversityInsights universityId={2} universityName="Second" />);
  expect(screen.queryByText(/No Key Insights have been added/)).toBeNull();
  expect(await screen.findByText("<script>unsafe()</script>")).toBeTruthy();
  expect(view.container.querySelector("script")).toBeNull();
  expect(fetcher.mock.calls[2][0]).toContain("/universities/2/insights");
});
