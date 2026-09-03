import { afterEach } from "vitest"

import { assertOpenDialogsHaveAccessibleContext } from "./dialog-accessibility"

if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

afterEach(() => {
  if (typeof document !== "undefined") {
    assertOpenDialogsHaveAccessibleContext()
  }
})