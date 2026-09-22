// @vitest-environment jsdom

import { describe, expect, it, vi } from "vitest";
import {
  checkForFrontendRelease,
  findHashedEntryScript,
} from "./frontend-release-guard";

describe("frontend release guard", () => {
  it("finds the production entry script and ignores development modules", () => {
    document.head.innerHTML = `
      <script type="module" src="/src/main.tsx"></script>
      <script type="module" src="/portal/assets/index-current_1.js"></script>
    `;

    expect(findHashedEntryScript(document, "https://portal.example/portal/"))
      .toBe("https://portal.example/portal/assets/index-current_1.js");
  });

  it("reloads when the cache-busted index points to a newer bundle", async () => {
    const reload = vi.fn();
    const fetcher = vi.fn().mockResolvedValue(new Response(
      '<script type="module" src="/assets/index-new.js"></script>',
      { status: 200, headers: { "Content-Type": "text/html" } },
    ));

    await expect(checkForFrontendRelease({
      currentEntry: "https://portal.example/assets/index-old.js",
      indexUrl: "https://portal.example/",
      fetcher,
      reload,
    })).resolves.toBe(true);

    expect(reload).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls[0][0]).toContain("__frontend_release_check=");
    expect(fetcher.mock.calls[0][1]).toMatchObject({ cache: "no-store" });
  });

  it("keeps the current page running when the bundle is unchanged", async () => {
    const reload = vi.fn();
    const fetcher = vi.fn().mockResolvedValue(new Response(
      '<script type="module" src="/assets/index-current.js"></script>',
      { status: 200 },
    ));

    await expect(checkForFrontendRelease({
      currentEntry: "https://portal.example/assets/index-current.js",
      indexUrl: "https://portal.example/",
      fetcher,
      reload,
    })).resolves.toBe(false);

    expect(reload).not.toHaveBeenCalled();
  });
});