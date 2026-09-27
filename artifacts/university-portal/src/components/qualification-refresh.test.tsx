// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { QualificationRefresh } from "./qualification-refresh";

vi.mock("@/components/can", () => ({ Can: ({ children }: { children: React.ReactNode }) => children }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const preview = {
  token: "signed-preview", courseIds: [1, 2, 3], sourceUrl: "https://www.law.ac.uk/", message: "Preview only",
  changes: [
    { award: "PG Cert", campus: "London Moorgate", kind: "retained", stagedId: 3,
      old: { amount: 6600, year: 2026, term: "Full Course" },
      new: { amount: 6750, year: 2027, term: "Full Course" }, intakes: [] },
    { award: "PG Cert", campus: "Bristol", kind: "new", stagedId: null, old: null,
      new: { amount: 6300, year: 2027, term: "Full Course" },
      intakes: [{ intake: "October 2027", study_load: "Part-time", locations: ["Bristol"] }] },
  ],
};

function setup() {
  const onApplied = vi.fn().mockResolvedValue(undefined);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
    <QualificationRefresh courseId={3} metadata={{ ulaw_qualification_scope: { award: "PG Cert" } }} onApplied={onApplied} />
  </QueryClientProvider>);
  return onApplied;
}

it("requires a read-only preview then explicit apply and refreshes the Review list", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json(preview))
    .mockResolvedValueOnce(Response.json({ status: "applied", courseIds: [1, 2, 3, 4] }));
  vi.stubGlobal("fetch", fetcher);
  const onApplied = setup();
  fireEvent.click(screen.getByText("Preview current award cohort"));
  expect(await screen.findByText(/Bristol · New campus/)).toBeTruthy();
  expect(screen.getByText(/Before: GBP 6,600/).textContent).toContain("2026");
  expect(screen.getByText(/After: GBP 6,750/).textContent).toContain("Full Course");
  expect(screen.getByText(/October 2027/)).toBeTruthy();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(String(fetcher.mock.calls[0][0])).toContain("/3/qualification-refresh/preview");
  fireEvent.click(screen.getByText("Apply to staging only"));
  await waitFor(() => expect(onApplied).toHaveBeenCalledOnce());
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ token: "signed-preview" });
  expect(await screen.findByText(/Nothing was published/)).toBeTruthy();
  expect(screen.queryByText("Apply to staging only")).toBeNull();
});

it("shows actionable stale-source error without claiming success", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(Response.json(preview))
    .mockResolvedValueOnce(Response.json({ detail: "Official source changed since preview. Preview again before applying." }, { status: 409 })));
  const onApplied = setup();
  fireEvent.click(screen.getByText("Preview current award cohort"));
  fireEvent.click(await screen.findByText("Apply to staging only"));
  expect((await screen.findByRole("alert")).textContent).toContain("Retry preview");
  expect(onApplied).not.toHaveBeenCalled();
  expect(screen.getByText("Refresh preview")).toBeTruthy();
});