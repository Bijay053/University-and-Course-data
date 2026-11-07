// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/context/auth", () => ({ useAuth: () => ({ user: { id: 1, email: "a@uni.test" } }) }));
vi.mock("./assistant-import-status", () => ({ AssistantImportStatus: () => null }));

import { ScrapePromptAssistant, type AssistantThread } from "./scrape-prompt-assistant";
import { DocumentReviewView } from "./assistant-document-review";

it("renders numeric backend row IDs and distinguishes unchecked courses", () => {
  const view = render(<DocumentReviewView testId="review" review={{
    checked: 1, proposed: 1, unmatched: 0, notChecked: 527,
    rows: [{ id: 56052, name: "BSc Accounting", status: "proposed",
      changes: [{ field: "ielts_reading", oldValue: null, newValue: 5.5 }] }],
  }} />);
  expect(screen.getByText("BSc Accounting")).toBeTruthy();
  expect(screen.getByTestId("review-counts").textContent).toContain("527 not checked");
  view.rerender(<DocumentReviewView testId="review" review={{
    checked: 0, proposed: 0, unmatched: 0, notChecked: 528, rows: [],
  }} />);
  expect(screen.getByTestId("review-counts").textContent).toContain("0 checked");
  expect(screen.getByTestId("review-counts").textContent).toContain("528 not checked");
  expect(screen.getByText("No course rows were checked against this document.")).toBeTruthy();
});

it("shows one access failure instead of repeating it for every selected course", () => {
  render(<DocumentReviewView testId="blocked-review" review={{
    checked: 0, proposed: 0, unmatched: 0, notChecked: 227,
    accessError: {
      message: "Cloudflare challenged robots.txt; the document was not requested.",
      alternatives: ["Upload an authorised document.", "Request crawler authorisation."],
    },
    rows: Array.from({ length: 227 }, (_, i) => ({
      id: i, name: `Course ${i}`, status: "not_checked",
      reason: "Cloudflare challenged robots.txt", applicability: "unverified",
    })),
  }} />);
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  expect(screen.getAllByText(/Cloudflare challenged robots.txt/)).toHaveLength(1);
  expect(screen.getByText(/227 selected records were not checked/)).toBeTruthy();
  expect(screen.queryByText(/Applicability: unverified/)).toBeNull();
  expect(screen.getByText("Upload an authorised document.")).toBeTruthy();
});

const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
const empty: AssistantThread = { threadId: null, status: "idle", phase: "", error: null, messages: [] };
const pdf = (size = 10) => new File([new Uint8Array(size)], "fees.pdf", { type: "application/pdf" });
const grant = { uploadUrl: "https://bucket.s3.test/", fields: { key: "k1", policy: "p" }, documentToken: "tok-1" };

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

async function setup(postResult: () => Response) {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    const u = String(url);
    if (u.endsWith("/documents/upload-url")) return json(grant);
    if (u.startsWith("https://bucket")) return new Response(null, { status: 204 });
    if (init?.method === "POST") return postResult();
    return json(empty);
  });
  render(<ScrapePromptAssistant jobId="j1" />);
  const input = await screen.findByTestId("input-assistant-prompt");
  return { fetchMock, input };
}

describe("document upload", () => {
  it("uploads directly to S3, sends only the token and clears on success", async () => {
    const { fetchMock, input } = await setup(() => json({ ...empty, status: "ready" }));
    fireEvent.change(screen.getByTestId("input-assistant-document"), { target: { files: [pdf()] } });
    await waitFor(() => expect(screen.getByTestId("assistant-attachment").dataset.status).toBe("ready"));
    const meta = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/documents/upload-url"))!;
    expect(meta[0]).toBe("/api/scrape/jobs/j1/assistant/documents/upload-url");
    expect(JSON.parse(String(meta[1]?.body))).toEqual({ filename: "fees.pdf", size: 10, contentType: "application/pdf" });
    const s3 = fetchMock.mock.calls.find(([u]) => String(u).startsWith("https://bucket"))!;
    const fd = s3[1]?.body as FormData;
    expect(fd.get("key")).toBe("k1");
    expect(fd.get("file")).toBeInstanceOf(File);
    fireEvent.change(input, { target: { value: "Check fees" } });
    fireEvent.click(screen.getByTestId("button-assistant-submit"));
    await waitFor(() => expect(screen.queryByTestId("assistant-attachment")).toBeNull());
    const post = fetchMock.mock.calls.find(([u, i]) => u === "/api/scrape/jobs/j1/assistant" && i?.method === "POST")!;
    expect(JSON.parse(String(post[1]?.body))).toEqual({ prompt: "Check fees", documentToken: "tok-1" });
  });

  it("keeps the attachment when submit fails", async () => {
    const { input } = await setup(() => json({ error: "x" }, 500));
    fireEvent.change(screen.getByTestId("input-assistant-document"), { target: { files: [pdf()] } });
    await waitFor(() => expect(screen.getByTestId("assistant-attachment").dataset.status).toBe("ready"));
    fireEvent.change(input, { target: { value: "Check" } });
    fireEvent.click(screen.getByTestId("button-assistant-submit"));
    await screen.findByTestId("assistant-submit-error");
    expect(screen.getByTestId("assistant-attachment").dataset.status).toBe("ready");
  });

  it("rejects oversize or non-PDF files without any request", async () => {
    const { fetchMock } = await setup(() => json(empty));
    const calls = fetchMock.mock.calls.length;
    fireEvent.change(screen.getByTestId("input-assistant-document"), { target: { files: [pdf(2_000_001)] } });
    expect(screen.getByTestId("text-attachment-status").textContent).toMatch(/2 MB/);
    fireEvent.change(screen.getByTestId("input-assistant-document"), { target: { files: [new File(["x"], "a.txt", { type: "text/plain" })] } });
    expect(screen.getByTestId("text-attachment-status").textContent).toMatch(/Only PDF/);
    expect(fetchMock.mock.calls.length).toBe(calls);
  });
});

describe("document limits and isolation", () => {
  it("accepts exactly 2,000,000 bytes", async () => {
    await setup(() => json(empty));
    fireEvent.change(screen.getByTestId("input-assistant-document"), { target: { files: [pdf(2_000_000)] } });
    await waitFor(() => expect(screen.getByTestId("assistant-attachment").dataset.status).toBe("ready"));
  });

  it("drops a late upload after a job switch", async () => {
    let finishS3: (r: Response) => void = () => {};
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const u = String(url);
      if (u.endsWith("/documents/upload-url")) return json(grant);
      if (u.startsWith("https://bucket")) return new Promise<Response>(r => { finishS3 = r; });
      return json(empty);
    });
    const { rerender } = render(<ScrapePromptAssistant jobId="j1" />);
    await screen.findByTestId("input-assistant-prompt");
    fireEvent.change(screen.getByTestId("input-assistant-document"), { target: { files: [pdf()] } });
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).startsWith("https://bucket"))).toBe(true));
    rerender(<ScrapePromptAssistant jobId="j2" />);
    await screen.findByTestId("input-assistant-prompt");
    finishS3(new Response(null, { status: 204 }));
    await new Promise(r => setTimeout(r, 20));
    expect(screen.queryByTestId("assistant-attachment")).toBeNull();
    rerender(<ScrapePromptAssistant jobId="j1" />);
    await screen.findByTestId("input-assistant-prompt");
    expect(screen.queryByTestId("assistant-attachment")).toBeNull();
  });
});

describe("document review", () => {
  it("renders every checked row and requires confirmation for document_update", async () => {
    const thread: AssistantThread = { ...empty, status: "ready", messages: [{ role: "assistant", content: "Reviewed.", createdAt: "2024-01-01T00:00:00Z",
      documentReview: { checked: 2, proposed: 1, unmatched: 1, rows: [
        { id: "r1", name: "BSc Nursing", status: "proposed", reason: "Fee differs", changes: [{ field: "international_fee", oldValue: "AUD 30,000", newValue: "AUD 32,400", quote: "Nursing AUD 32,400", page: 3 }] },
        { id: "r2", name: "MBA", status: "unmatched", reason: "Not found in document", changes: [] },
      ] },
      actions: [{ id: "doc", kind: "document_update", label: "Apply document values", state: "proposed", courseCount: 1, fields: ["international_fee"] }] }] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(thread));
    render(<ScrapePromptAssistant jobId="j1" />);
    expect((await screen.findByTestId("document-row-r1")).textContent).toContain("AUD 32,400");
    expect(screen.getByText("AUD 30,000")).toBeTruthy();
    expect(screen.getByText(/page 3/)).toBeTruthy();
    expect(screen.getByTestId("document-row-r2").textContent).toContain("Not found in document");
    expect(screen.getByText(/1 skipped \/ manual review/)).toBeTruthy();
    expect(screen.getByText(/This is a preview, not an update receipt/)).toBeTruthy();
    fireEvent.click(screen.getByTestId("button-run-action-doc"));
    expect(await screen.findByTestId("dialog-confirm-action")).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([, i]) => i?.method === "POST")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("button-confirm-action"));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => u === "/api/scrape/jobs/j1/assistant/actions/doc")).toBe(true));
  });
});
