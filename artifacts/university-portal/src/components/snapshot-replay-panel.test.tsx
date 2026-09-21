// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SnapshotReplayPanel } from "./snapshot-replay-panel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SnapshotReplayPanel", () => {
  it("shows backend audience review evidence inside a replay diff", async () => {
    const responses = [
      { total: 1, s3_enabled: true, db_records: [] },
      {
        job_id: "job-1", replayed: 1, changed: 0, unchanged: 1, errors: 0,
        commit: false, message: "Replay complete",
        diffs: [],
        audience_reviews: [{
          url: "https://example.edu/course",
          new_name: "Course",
          evidence: {
            status: "needs_review", same_panel: true, linked_official: true,
            evidence: [{ audience: "international", container: "audience", value: "intl", label: "International March", intake_months: [3], source_url: "https://example.edu/english" }],
            issues: ["image-only audience option"],
          },
          proposal: { status: "needs_review", reason: "audience evidence is unresolved", proposals: [] },
        }],
      },
    ];
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve({
      ok: true,
      json: () => Promise.resolve(responses.shift()),
    })));

    render(<SnapshotReplayPanel jobId="job-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Load Snapshots" }));
    await screen.findByText("1 snapshot in DB");
    fireEvent.click(screen.getByRole("button", { name: "▶ Replay (diff only)" }));
    await screen.findByText("Course");
    await waitFor(() => expect(screen.getByText("Selected international option")).toBeTruthy());
    expect(screen.getByText("Why this needs review:").parentElement?.textContent)
      .toContain("image-only audience option; audience evidence is unresolved");
  });
});