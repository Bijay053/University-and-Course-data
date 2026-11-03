// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Router, Switch, Route } from "wouter";
import { AuthProvider } from "@/context/auth";
import { AuthGuard } from "@/components/auth-guard";
import Login from "./login";

vi.mock("@workspace/api-client-react", () => ({ setUnauthorizedHandler: vi.fn() }));
vi.mock("@/hooks/use-toast", () => ({ toast: vi.fn() }));

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/login");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

function Routes() {
  return <Router><AuthProvider><Switch>
    <Route path="/login"><Login /></Route>
    <Route path="/toolkit"><h1>Public toolkit</h1></Route>
    <Route><AuthGuard><h1>Staff dashboard</h1></AuthGuard></Route>
  </Switch></AuthProvider></Router>;
}

function mockServer() {
  const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith("/me")) return new Response(JSON.stringify({ user: null }));
    if (path.endsWith("/login")) {
      expect(new Headers(options?.headers).get("X-Portal-Login-Flow")).toBe("email-otp-v1");
      return new Response(JSON.stringify({
        otp_required: true, challenge_token: "test-only-pending-challenge-token",
        masked_email: "p***@example.test", expires_in: 500, resend_after: 30,
      }));
    }
    if (path.endsWith("/verify-otp")) return new Response(JSON.stringify({
      token: "test-only-verified-token",
      user: { id: 1, email: "person@example.test", name: "Test", role: "admin" },
      permissions: [], is_super_admin: true,
    }));
    throw new Error("Unexpected API path");
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

describe("login navigation with the real authentication provider and route guard", () => {
  it("stays on login for a pending or recovered code, then enters the dashboard only after verification", async () => {
    mockServer();
    render(<Routes />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "test-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Send verification code" }));
    const code = await screen.findByLabelText("Email verification code");
    expect(window.location.pathname).toBe("/login");
    expect(screen.queryByText("Public toolkit")).toBeNull();
    expect(screen.queryByText("Staff dashboard")).toBeNull();
    expect(localStorage.getItem("uniportal_auth_token")).toBeNull();
    fireEvent.change(code, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and sign in" }));
    await screen.findByText("Staff dashboard");
    await waitFor(() => expect(window.location.pathname).toBe("/"));
    expect(screen.queryByText("Public toolkit")).toBeNull();
  });

  it("preserves anonymous toolkit access instead of exposing the dashboard", async () => {
    mockServer();
    window.history.replaceState(null, "", "/");
    render(<Routes />);
    await screen.findByText("Public toolkit");
    expect(window.location.pathname).toBe("/toolkit");
    expect(screen.queryByText("Staff dashboard")).toBeNull();
  });
});
