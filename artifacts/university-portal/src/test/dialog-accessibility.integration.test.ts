// @vitest-environment node

import { spawnSync } from "node:child_process"
import { fileURLToPath } from "node:url"

import { describe, expect, it } from "vitest"

describe("portal dialog accessibility setup", () => {
  it("fails a configured test run before local dialog cleanup", () => {
    const portalDirectory = fileURLToPath(new URL("../..", import.meta.url))
    const result = spawnSync(
      "pnpm",
      [
        "exec",
        "vitest",
        "run",
        "--config",
        "src/test/fixtures/vitest.config.ts",
      ],
      {
        cwd: portalDirectory,
        encoding: "utf8",
        env: { ...process.env, VITEST_MAX_THREADS: "1" },
      },
    )
    const output = `${result.stdout}\n${result.stderr}`

    expect(result.status).not.toBe(0)
    expect(output).toContain("Opened dialog 1 has no accessible description")
  })
})