import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const uiDirectory = dirname(fileURLToPath(import.meta.url));

describe("shared interactive cursor styles", () => {
  it("does not override clickable UI primitives with the default arrow cursor", () => {
    const files = readdirSync(uiDirectory).filter(
      (file) => file.endsWith(".tsx") && !file.endsWith(".test.tsx"),
    );

    for (const file of files) {
      const source = readFileSync(join(uiDirectory, file), "utf8");
      expect(source, file).not.toContain("cursor-default");
    }
  });

  it("uses the hand cursor on every shared select option", () => {
    const source = readFileSync(join(uiDirectory, "select.tsx"), "utf8");

    expect(source).toMatch(
      /SelectPrimitive\.Item[\s\S]*?cursor-pointer[\s\S]*?SelectPrimitive\.ItemText/,
    );
  });
});