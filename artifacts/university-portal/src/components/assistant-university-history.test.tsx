// @vitest-environment jsdom
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AssistantUniversityHistory } from "./assistant-university-history";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
const message = (index: number, extra = {}) => ({
  threadId: "thread", jobId: "current", position: index, role: "user",
  content: `Saved message ${index}`, createdAt: "2026-11-17T04:00:00Z", ...extra,
});
const page = (indices: number[], nextCursor: string | null = null) => ({
  messages: indices.map(i => message(i)), nextCursor,
  retentionNotice: "Only retained history can be shown; removed messages are not restored.",
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("university conversation history", () => {
  it("loads only on request, paginates older messages and never executes historical actions", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json({ ...page([22, 21], "cursor+value="), messages: [
        message(22, { actions: [{ id: "old", label: "Old merge", state: "proposed" }] }),
        message(21, { jobId: "earlier", content: "Earlier review conversation" }),
      ] }))
      .mockResolvedValueOnce(json(page([20, 19])));
    const openReview = vi.fn();
    render(<AssistantUniversityHistory jobId="current" onReviewJob={openReview} />);
    expect(fetcher).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "View university history" }));
    await screen.findByText("Earlier review conversation");
    fireEvent.click(screen.getByRole("button", { name: "Open original review" }));
    expect(openReview).toHaveBeenCalledWith("earlier");
    fireEvent.click(screen.getByRole("button", { name: "Load older messages" }));
    await screen.findByText("Saved message 19");
    expect(screen.getAllByTestId("university-history-message")).toHaveLength(4);
    expect(String(fetcher.mock.calls[1][0])).toContain("cursor=cursor%2Bvalue%3D");
    expect(screen.getByText("End of retained university history.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run now|Approve|Apply/ })).toBeNull();
    expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
    expect(fetcher.mock.calls[0][1]?.credentials).toBe("include");
  });

  it("preserves loaded history on a partial failure and retries the failed older page", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(page([21], "next")))
      .mockResolvedValueOnce(json({ detail: "History temporarily unavailable" }, 503))
      .mockResolvedValueOnce(json(page([20])));
    render(<AssistantUniversityHistory jobId="current" />);
    fireEvent.click(screen.getByRole("button", { name: "View university history" }));
    await screen.findByText("Saved message 21");
    fireEvent.click(screen.getByRole("button", { name: "Load older messages" }));
    await screen.findByRole("alert");
    expect(screen.getByText("Saved message 21")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry history" }));
    await screen.findByText("Saved message 20");
    expect(fetcher.mock.calls[1][0]).toEqual(fetcher.mock.calls[2][0]);
  });

  it("shows errors explicitly and refreshes from the newest page", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json({ detail: "Not available" }, 503))
      .mockResolvedValueOnce(json(page([1])))
      .mockResolvedValueOnce(json(page([2])));
    render(<AssistantUniversityHistory jobId="current" />);
    fireEvent.click(screen.getByRole("button", { name: "View university history" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Retry history" }));
    await screen.findByText("Saved message 1");
    fireEvent.click(screen.getByRole("button", { name: "Refresh history" }));
    await screen.findByText("Saved message 2");
    expect(screen.queryByText("Saved message 1")).toBeNull();
    expect(fetcher.mock.calls.every(([url]) => !String(url).includes("cursor="))).toBe(true);
  });

  it("renders saved evidence and original-review images safely", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({
      ...page([]), messages: [message(1, { jobId: "earlier", turnId: "original-turn",
        attachment: { filename: "course.png", contentType: "image/png" },
        sources: [{ title: "Official evidence", url: "https://example.edu/course", excerpt: "Original quotation." },
          { title: "Unsafe link", url: "javascript:alert(1)", excerpt: "<script>not executable</script>" }],
      })],
    }));
    render(<AssistantUniversityHistory jobId="current" />);
    fireEvent.click(screen.getByRole("button", { name: "View university history" }));
    await screen.findByText("Saved message 1");
    expect(screen.getByRole("img").getAttribute("src")).toContain("/jobs/earlier/assistant/messages/original-turn/image");
    expect(screen.getByRole("link", { name: "Official evidence", hidden: true }).getAttribute("href")).toBe("https://example.edu/course");
    expect(document.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(document.querySelector("script")).toBeNull();
  });

  it("clears history on identity/review remount and ignores a late response", async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi.spyOn(globalThis, "fetch").mockImplementationOnce(() =>
      new Promise<Response>(resolve => { finish = resolve; }))
      .mockResolvedValueOnce(json(page([])));
    const { rerender } = render(<AssistantUniversityHistory key="old-user:old-review" jobId="old" />);
    fireEvent.click(screen.getByRole("button", { name: "View university history" }));
    rerender(<AssistantUniversityHistory key="new-user:new-review" jobId="new" />);
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true);
    finish(json(page([99])));
    fireEvent.click(screen.getByRole("button", { name: "View university history" }));
    await screen.findByText("No saved messages were found for your account at this university.");
    expect(screen.queryByText("Saved message 99")).toBeNull();
  });

  it("does not duplicate entries or fetch twice on rapid clicks", async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(page([21], "next")))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }));
    render(<AssistantUniversityHistory jobId="current" />);
    fireEvent.click(screen.getByRole("button", { name: "View university history" }));
    await screen.findByText("Saved message 21");
    const button = screen.getByRole("button", { name: "Load older messages" });
    fireEvent.click(button);
    fireEvent.click(button);
    finish(json(page([21, 20])));
    await screen.findByText("Saved message 20");
    expect(screen.getAllByTestId("university-history-message")).toHaveLength(2);
    expect(fetcher).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });
});
