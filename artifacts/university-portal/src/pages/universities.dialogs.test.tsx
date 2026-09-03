// @vitest-environment jsdom

import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, it, vi } from "vitest";

import Universities from "./universities";

vi.mock("@workspace/api-client-react", () => ({
  getListUniversitiesQueryKey: () => ["universities"],
  useCreateUniversity: () => ({ mutate: vi.fn(), isPending: false }),
  useListUniversities: () => ({
    data: {
      data: [{
        id: 7,
        name: "Accessible University",
        city: "Sydney",
        country: "Australia",
        website: "https://example.edu",
        courseCount: 12,
        featured: false,
      }],
    },
    isLoading: false,
  }),
}));

vi.mock("@/components/can", () => ({
  Can: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useCan: () => ({ can: () => true, canAny: () => true }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/components/ui/dropdown-menu", () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuItem: ({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) => (
    <button role="menuitem" onClick={onClick}>{children}</button>
  ),
  DropdownMenuSeparator: () => <hr />,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

afterEach(cleanup);

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Universities />
    </QueryClientProvider>,
  );
}

describe("Universities dialogs", () => {
  it("opens the create dialog", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add University" }));
    await screen.findByRole("dialog", {
      name: "Add New University",
      description: /enter the institution details/i,
    });
  });

  it("opens the edit dialog", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("menuitem", { name: "Edit Details" }));
    await screen.findByRole("dialog", {
      name: "Edit University",
      description: /update the institution name/i,
    });
  });

  it("opens the delete dialog", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    await screen.findByRole("dialog", {
      name: "Delete University",
      description: /confirm permanent removal/i,
    });
  });

  it("opens the featured confirmation dialog", async () => {
    renderPage();
    fireEvent.click(screen.getByTitle("Mark as Featured"));
    await screen.findByRole("dialog", {
      name: "Mark as Featured",
      description: /featured search results/i,
    });
  });
});