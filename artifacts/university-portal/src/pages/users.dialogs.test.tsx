// @vitest-environment jsdom

import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, it, vi } from "vitest";

import UsersPage from "./users";

vi.mock("@/context/auth", () => ({
  useAuth: () => ({ user: { id: 1, email: "admin@example.test" } }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  fetchWithAuth: vi.fn(async (url: string) => {
    const body = url.endsWith("/api/users")
      ? [{ id: 2, email: "operator@example.test", full_name: "Operator", is_active: true, is_super_admin: false, role_id: null, role_name: null }]
      : url.endsWith("/api/roles")
        ? []
        : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }),
}));

afterEach(cleanup);

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <UsersPage />
    </QueryClientProvider>,
  );
}

describe("Users dialogs", () => {
  it("opens the create, edit, status, and delete dialogs", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("operator@example.test");

    await user.click(screen.getByRole("button", { name: "Add user" }));
    const createDialog = await screen.findByRole("dialog", {
      name: "Add user",
      description: "Create a new team member account.",
    });
    await user.click(within(createDialog).getByRole("button", { name: "Cancel" }));

    const row = screen.getByText("operator@example.test").closest("tr")!;
    const rowButtons = within(row).getAllByRole("button");

    await user.click(rowButtons[1]);
    const editDialog = await screen.findByRole("dialog", {
      name: "Edit user",
      description: "operator@example.test",
    });
    await user.click(within(editDialog).getByRole("button", { name: "Cancel" }));

    await user.click(within(row).getByRole("button", { name: "Set operator@example.test inactive" }));
    const statusDialog = await screen.findByRole("alertdialog", {
      name: "Set user inactive?",
      description: /will no longer be able to sign in/i,
    });
    await user.click(within(statusDialog).getByRole("button", { name: "Cancel" }));

    await user.click(rowButtons[3]);
    await screen.findByRole("alertdialog", {
      name: "Delete user?",
      description: /permanently removes operator@example.test/i,
    });
  });
});