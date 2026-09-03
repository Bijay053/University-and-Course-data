import path from "node:path";

export const sourceAliases = {
  "@": path.resolve(import.meta.dirname, "src"),
} as const;