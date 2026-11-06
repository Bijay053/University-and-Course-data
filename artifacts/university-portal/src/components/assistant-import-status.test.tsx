// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AssistantImportStatus } from "./assistant-import-status";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {status});
const paused = {paused: true, canResume: true, universityName: "Excelsia", reason: "Consolidation must be complete."};

it("requires consolidation confirmation and refreshes resumed status", async () => {
  const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(paused))
    .mockResolvedValueOnce(json({...paused, paused: false}));
  const onStatus = vi.fn();
  render(<AssistantImportStatus base="/api/test" onStatus={onStatus} />);
  await screen.findByText(/Assistant imports paused/);
  fireEvent.click(screen.getByText("Review resume requirements"));
  expect((screen.getByText("Resume imports") as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(screen.getByRole("textbox"), {target: {value: "Duplicate groups reviewed and resolved"}});
  fireEvent.click(screen.getByText("Resume imports"));
  await screen.findByText(/Assistant imports enabled/);
  expect(onStatus).toHaveBeenLastCalledWith(false);
  expect(JSON.parse(fetcher.mock.calls[1][1]?.body as string).consolidationComplete).toBe(true);
});

it("shows paused state without resume controls to staff", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json({...paused, canResume: false}));
  render(<AssistantImportStatus base="/api/test" onStatus={vi.fn()} />);
  await screen.findByText(/Only an owner administrator/);
  expect(screen.queryByText("Review resume requirements")).toBeNull();
});

it("keeps the hold visible when resume is denied", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(paused))
    .mockResolvedValueOnce(json({detail: "Permission denied"}, 403));
  render(<AssistantImportStatus base="/api/test" onStatus={vi.fn()} />);
  fireEvent.click(await screen.findByText("Review resume requirements"));
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(screen.getByRole("textbox"), {target: {value: "Consolidation reviewed completely"}});
  fireEvent.click(screen.getByText("Resume imports"));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Permission denied"));
  expect(screen.queryByText(/Assistant imports enabled/)).toBeNull();
});
