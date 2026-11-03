// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReportCourseErrorForm } from "./report-course-error";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>{ui}</QueryClientProvider>);
const form = (id = 7) => <ReportCourseErrorForm courseId={id} courseName="Master of IT" universityName="Kent Uni" />;

describe("ReportCourseErrorForm", () => {
  it("reports the university insight and country rather than a course", async () => {
    const f = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 9, status: "new" }), { status: 201 }));
    vi.stubGlobal("fetch", f);
    wrap(<ReportCourseErrorForm courseName="Key Insights — Nepal" universityName="Harbour Uni"
      insight={{ universityId: 4, id: 8, country: "Nepal" }} />);
    expect(screen.getByText("Key Insights — Nepal")).toBeTruthy();
    expect(screen.queryByTestId("select-report-field")).toBeNull();
    fireEvent.change(screen.getByTestId("input-report-message"), { target: { value: "The scholarship amount is incorrect." } });
    fireEvent.click(screen.getByTestId("button-report-submit"));
    await screen.findByTestId("status-report-success");
    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({
      university_id: 4, insight_id: 8, kind: "incorrect", field: "key_insights",
      message: "The scholarship amount is incorrect.",
    });
  });
  it("validates description and source url without submitting", () => {
    const f = vi.fn(); vi.stubGlobal("fetch", f);
    wrap(form());
    fireEvent.change(screen.getByTestId("input-report-message"), { target: { value: "short" } });
    fireEvent.change(screen.getByTestId("input-report-source"), { target: { value: "ftp://x" } });
    fireEvent.click(screen.getByTestId("button-report-submit"));
    expect(screen.getByTestId("error-report-message").textContent).toMatch(/at least 10/);
    expect(screen.getByTestId("error-report-source")).toBeTruthy();
    expect(f).not.toHaveBeenCalled();
    fireEvent.change(screen.getByTestId("input-report-message"), { target: { value: "A required English score is missing." } });
    fireEvent.change(screen.getByTestId("input-report-source"), { target: { value: "https://example.edu/course" } });
    expect(screen.getByTestId("error-report-message").textContent).not.toMatch(/at least 10 characters/);
    expect(screen.queryByTestId("error-report-source")).toBeNull();
  });

  it("submits payload once and shows success", async () => {
    let resolve!: (r: Response) => void;
    const f = vi.fn(() => new Promise<Response>((r) => { resolve = r; })); vi.stubGlobal("fetch", f);
    wrap(form());
    fireEvent.click(screen.getByTestId("radio-kind-missing"));
    fireEvent.change(screen.getByTestId("select-report-field"), { target: { value: "intakes" } });
    fireEvent.change(screen.getByTestId("input-report-message"), { target: { value: "  July intake is missing here " } });
    fireEvent.change(screen.getByTestId("input-report-source"), { target: { value: "https://uni.example/c" } });
    fireEvent.click(screen.getByTestId("button-report-submit"));
    fireEvent.click(screen.getByTestId("button-report-submit"));
    await waitFor(() => expect(f).toHaveBeenCalledTimes(1));
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/api\/course-error-reports$/);
    expect(JSON.parse(init.body as string)).toEqual({ course_id: 7, kind: "missing", field: "intakes", message: "July intake is missing here", source_url: "https://uni.example/c" });
    resolve(new Response(JSON.stringify({ id: 1, status: "new" }), { status: 201 }));
    await screen.findByTestId("status-report-success");
  });

  it("keeps details on network error and retries", async () => {
    const f = vi.fn().mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 2, status: "new" }), { status: 201 }));
    vi.stubGlobal("fetch", f);
    wrap(form());
    fireEvent.change(screen.getByTestId("input-report-message"), { target: { value: "Fee is wrong for 2025" } });
    fireEvent.click(screen.getByTestId("button-report-submit"));
    await screen.findByTestId("error-report-submit");
    expect((screen.getByTestId("input-report-message") as HTMLTextAreaElement).value).toBe("Fee is wrong for 2025");
    fireEvent.click(screen.getByTestId("button-report-submit"));
    await screen.findByTestId("status-report-success");
  });

  it("resets when course changes", () => {
    vi.stubGlobal("fetch", vi.fn());
    const { rerender } = wrap(form(1));
    fireEvent.change(screen.getByTestId("input-report-message"), { target: { value: "Something typed here" } });
    rerender(<QueryClientProvider client={new QueryClient()}>{form(2)}</QueryClientProvider>);
    expect((screen.getByTestId("input-report-message") as HTMLTextAreaElement).value).toBe("");
  });
});
