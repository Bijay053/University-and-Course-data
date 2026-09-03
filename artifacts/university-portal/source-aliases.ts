import path from "node:path";

const sourceAliasDirectories = {
  "@": "src",
} as const;

export const sourceAliases = Object.fromEntries(
  Object.entries(sourceAliasDirectories).map(([alias, directory]) => [
    alias,
    path.resolve(import.meta.dirname, directory),
  ]),
) as Record<keyof typeof sourceAliasDirectories, string>;

export const sourceAliasTypeScriptPaths = Object.fromEntries(
  Object.entries(sourceAliasDirectories).map(([alias, directory]) => [
    `${alias}/*`,
    [`./${directory}/*`],
  ]),
) as Record<`${keyof typeof sourceAliasDirectories}/*`, [string]>;
