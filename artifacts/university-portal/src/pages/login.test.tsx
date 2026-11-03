// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Login from "./login";

const mocks = vi.hoisted(() => ({
  login: vi.fn(), verifyOtp: vi.fn(), navigate: vi.fn(),
}));
vi.mock("@/context/auth", () => ({ useAuth: () => mocks }));
vi.mock("wouter", () => ({
  useLocation: () => ["/login", mocks.navigate],
  Link: ({ children, href, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => <a href={href} {...props}>{children}</a>,
}));
beforeEach(() => {
  vi.clearAllMocks();
  mocks.login.mockResolvedValue({
    challenge_token: "test-only-opaque-challenge-token-not-a-session", masked_email: "p***@example.test",
    expires_in: 600, resend_after: 60,
  });
  mocks.verifyOtp.mockResolvedValue(undefined);
});
afterEach(cleanup);

async function sendCode() {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.test" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "test-password" } });
  fireEvent.click(screen.getByRole("button", { name: "Send verification code" }));
  return await screen.findByLabelText("Email verification code");
}

describe("mandatory email verification", () => {
  it("does not navigate after password validation and verifies the code before entering the portal", async () => {
    render(<Login />);
    const input = await sendCode();
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Password")).toBeNull();
    expect(screen.getByText(/p\*\*\*@example.test/)).toBeTruthy();
    expect((screen.getByRole("button", { name: /Resend code in/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and sign in" }));
    await vi.waitFor(() => expect(mocks.navigate).toHaveBeenCalledWith("/"));
    expect(mocks.verifyOtp).toHaveBeenCalledWith("test-only-opaque-challenge-token-not-a-session", "123456");
  });

  it("keeps users on the verification step when a code is rejected", async () => {
    mocks.verifyOtp.mockRejectedValueOnce(new Error("Invalid or expired verification code"));
    render(<Login />);
    const input = await sendCode();
    fireEvent.change(input, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and sign in" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Invalid or expired");
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Email verification code")).toBeTruthy();
  });

  it("does not offer a code entry or sign users in when delivery fails", async () => {
    mocks.login.mockRejectedValueOnce(new Error("Could not send your verification email"));
    render(<Login />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "test-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Send verification code" }));
    await screen.findByRole("alert");
    expect(screen.queryByLabelText("Email verification code")).toBeNull();
    expect(mocks.navigate).not.toHaveBeenCalled();
  });
});
