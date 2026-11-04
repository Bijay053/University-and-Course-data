// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Route, Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { Layout } from "./layout";

const logout = vi.fn();
vi.mock("@/context/auth", () => ({
  useAuth: () => ({
    user: { name: "Admin", email: "admin@example.invalid" },
    can: () => true,
    logout,
  }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("dashboard Toolkit shortcut", () => {
  it("places a link to the existing public toolkit before the account menu", () => {
    render(<Layout><h1>Dashboard</h1></Layout>);
    const header = screen.getByRole("banner");
    const link = within(header).getByRole("link", { name: "Toolkit" });
    const account = within(header).getByRole("button", { name: "Open account menu for Admin" });
    expect(link.getAttribute("href")).toBe("/toolkit");
    expect(link.compareDocumentPosition(account) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeTruthy();
  });

  it("navigates to the toolkit without signing out", () => {
    const location = memoryLocation({ path: "/" });
    render(
      <Router hook={location.hook}>
        <Layout><h1>Dashboard</h1></Layout>
        <Route path="/toolkit"><h1>Public Toolkit</h1></Route>
      </Router>,
    );
    fireEvent.click(within(screen.getByRole("banner")).getByRole("link", { name: "Toolkit" }));
    expect(screen.getByRole("heading", { name: "Public Toolkit" })).toBeTruthy();
    expect(logout).not.toHaveBeenCalled();
  });
});
