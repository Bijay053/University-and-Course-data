import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { sourceAliasTypeScriptPaths } from "../source-aliases";

const alignmentMessage =
  "TypeScript source aliases are out of alignment: tsconfig.json compilerOptions.paths must match source-aliases.ts. Update the shared alias definition and TypeScript paths together.";

export async function checkSourceAliases(
  tsconfigPath: string | URL = new URL("../tsconfig.json", import.meta.url),
): Promise<void> {
  const tsconfig = JSON.parse(await readFile(tsconfigPath, "utf8")) as {
    compilerOptions?: {
      paths?: Record<string, string[]>;
    };
  };

  assert.deepEqual(
    tsconfig.compilerOptions?.paths,
    sourceAliasTypeScriptPaths,
    alignmentMessage,
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await checkSourceAliases();
}
