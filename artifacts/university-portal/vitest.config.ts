import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { sourceAliases } from "./source-aliases";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: sourceAliases,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test/setup.ts"],
    sequence: {
      hooks: "list",
    },
  },
});
