import { defineConfig } from "vitest/config"

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/test/fixtures/inaccessible-dialog.fixture.tsx"],
    setupFiles: ["./src/test/setup.ts"],
    sequence: {
      hooks: "list",
    },
  },
})