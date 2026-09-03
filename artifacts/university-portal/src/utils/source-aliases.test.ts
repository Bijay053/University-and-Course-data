import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { checkSourceAliases } from "../../scripts/check-source-aliases";

const temporaryDirectories: string[] = [];

async function writeTemporaryConfig(paths: Record<string, string[]>) {
  const directory = await mkdtemp(path.join(tmpdir(), "source-aliases-"));
  temporaryDirectories.push(directory);
  const configPath = path.join(directory, "tsconfig.json");
  await writeFile(configPath, JSON.stringify({ compilerOptions: { paths } }));
  return configPath;
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) =>
      rm(directory, { recursive: true, force: true }),
    ),
  );
});

describe("source alias alignment safeguard", () => {
  it("accepts matching runtime aliases and TypeScript paths", async () => {
    const configPath = await writeTemporaryConfig({ "@/*": ["./src/*"] });

    await expect(checkSourceAliases(configPath)).resolves.toBeUndefined();
  });

  it.each([
    ["renamed", { "~/*": ["./src/*"] }],
    ["added", { "@/*": ["./src/*"], "#/*": ["./shared/*"] }],
    ["missing", {}],
  ])("rejects a %s TypeScript mapping with repair guidance", async (_, paths) => {
    const configPath = await writeTemporaryConfig(paths);

    await expect(checkSourceAliases(configPath)).rejects.toThrow(
      /TypeScript source aliases are out of alignment.*Update the shared alias definition and TypeScript paths together/,
    );
  });
});