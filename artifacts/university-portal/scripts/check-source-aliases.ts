import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { sourceAliasTypeScriptPaths } from "../source-aliases";

const tsconfigPath = new URL("../tsconfig.json", import.meta.url);
const tsconfig = JSON.parse(await readFile(tsconfigPath, "utf8")) as {
  compilerOptions?: {
    paths?: Record<string, string[]>;
  };
};

assert.deepEqual(
  tsconfig.compilerOptions?.paths,
  sourceAliasTypeScriptPaths,
  "tsconfig.json compilerOptions.paths must match source-aliases.ts. Update the shared alias definition and TypeScript paths together.",
);
