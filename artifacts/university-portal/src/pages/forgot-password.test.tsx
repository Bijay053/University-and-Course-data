// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ForgotPassword from "./forgot-password";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function submitRequest() {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.test" } });
  fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));
}

describe("password recovery delivery", () => {
  it("shows delivery-unavailable errors without pretending an email was sent, and permits retry", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: "Password reset email is currently unavailable. Please contact the portal administrator.",
      }), { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal("fetch", fetcher);
    render(<ForgotPassword />);
    submitRequest();
    expect((await screen.findByRole("alert")).textContent).toContain("currently unavailable");
    expect(screen.queryByText(/check your inbox/i)).toBeNull();
    await waitFor(() => expect((screen.getByRole("button", { name: "Send reset link" }) as HTMLButtonElement).disabled).toBe(false));
    submitRequest();
    expect(await screen.findByText(/If an active account uses this email/)).toBeTruthy();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ email: "person@example.test" });
  });

  it("never renders reset credentials returned by an older backend", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: true, debug_reset_url: "https://example.test/reset-password?token=not-a-real-token",
    }))));
    render(<ForgotPassword />);
    submitRequest();
    await screen.findByText(/If an active account uses this email/);
    expect(screen.queryByText(/Dev mode/)).toBeNull();
    expect(document.querySelector('a[href*="token="]')).toBeNull();
    expect(document.body.textContent).not.toContain("not-a-real-token");
  });
});
