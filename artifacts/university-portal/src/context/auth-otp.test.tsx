// @vitest-environment jsdom
import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "./auth";

vi.mock("@workspace/api-client-react", () => ({ setUnauthorizedHandler: vi.fn() }));
vi.mock("@/hooks/use-toast", () => ({ toast: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

function Probe({ legacy = false }: { legacy?: boolean }) {
  const auth = useAuth();
  const [challenge, setChallenge] = useState("");
  const [error, setError] = useState("");
  return <>
    <p>{auth.user ? "Signed in" : "Signed out"}</p>
    <p role="status">{challenge ? "Code required" : ""}</p>
    <p role="alert">{error}</p>
    <button onClick={async () => {
      try {
        const result = await auth.login("person@example.test", "test-password",
          legacy ? undefined as unknown as "email-otp-v1" : "email-otp-v1");
        setChallenge(result.challenge_token);
      } catch (reason) { setError((reason as Error).message); }
    }}>Start login</button>
    <button disabled={!challenge} onClick={() => auth.verifyOtp(challenge, "123456")}>Verify</button>
  </>;
}

const challengeResponse = {
  otp_required: true, challenge_token: "test-only-opaque-preauthentication-proof",
  masked_email: "p***@example.test", expires_in: 600, resend_after: 60,
};
const verifiedResponse = {
  token: "test-only-verified-session",
  user: { id: 777, email: "person@example.test", name: "Test", role: "user" },
  permissions: ["universities.view"], is_super_admin: false,
};

describe("authentication state requires a verified OTP", () => {
  it("rejects an older open form even when its auth provider has hot-reloaded", async () => {
    const fetcher = vi.fn(async (_path: string) => new Response(JSON.stringify({ user: null })));
    vi.stubGlobal("fetch", fetcher);
    render(<AuthProvider><Probe legacy /></AuthProvider>);
    fireEvent.click(screen.getByText("Start login"));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("reload this page"));
    expect(fetcher.mock.calls.every((call) => !String(call[0]).endsWith("/login"))).toBe(true);
    expect(screen.getByText("Signed out")).toBeTruthy();
  });
  it("stores no authentication token until verification succeeds", async () => {
    vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(JSON.stringify(
      path.endsWith("/me") ? { user: null } :
        path.endsWith("/login") ? challengeResponse : verifiedResponse,
    ))));
    render(<AuthProvider><Probe /></AuthProvider>);
    fireEvent.click(screen.getByText("Start login"));
    await screen.findByText("Code required");
    expect(localStorage.getItem("uniportal_auth_token")).toBeNull();
    expect(screen.getByText("Signed out")).toBeTruthy();
    fireEvent.click(screen.getByText("Verify"));
    await screen.findByText("Signed in");
    expect(localStorage.getItem("uniportal_auth_token")).toBe("test-only-verified-session");
  });

  it("refuses a legacy backend response that skips OTP", async () => {
    vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(JSON.stringify(
      path.endsWith("/me") ? { user: null } : verifiedResponse,
    ))));
    render(<AuthProvider><Probe /></AuthProvider>);
    fireEvent.click(screen.getByText("Start login"));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Email verification is required"));
    expect(localStorage.getItem("uniportal_auth_token")).toBeNull();
    expect(screen.getByText("Signed out")).toBeTruthy();
  });

  it("ignores an earlier session refresh that finishes after verification", async () => {
    let finishRefresh!: (response: Response) => void;
    const refreshResponse = new Promise<Response>((resolve) => { finishRefresh = resolve; });
    vi.stubGlobal("fetch", vi.fn((path: string) => path.endsWith("/me") ? refreshResponse :
      Promise.resolve(new Response(JSON.stringify(path.endsWith("/login") ? challengeResponse : verifiedResponse)))));
    render(<AuthProvider><Probe /></AuthProvider>);
    fireEvent.click(screen.getByText("Start login"));
    await screen.findByText("Code required");
    fireEvent.click(screen.getByText("Verify"));
    await screen.findByText("Signed in");
    await act(async () => {
      finishRefresh(new Response(JSON.stringify({ user: null })));
      await refreshResponse;
    });
    await waitFor(() => expect(screen.getByText("Signed in")).toBeTruthy());
    expect(localStorage.getItem("uniportal_auth_token")).toBe("test-only-verified-session");
  });
});
