import { afterEach } from "vitest"

import { assertOpenDialogsHaveAccessibleContext } from "./dialog-accessibility"

afterEach(() => {
  if (typeof document !== "undefined") {
    assertOpenDialogsHaveAccessibleContext()
  }
})