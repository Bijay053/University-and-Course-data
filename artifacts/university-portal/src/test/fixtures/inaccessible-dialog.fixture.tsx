// @vitest-environment jsdom

import React from "react"
import { cleanup, render } from "@testing-library/react"
import { afterEach, it } from "vitest"

afterEach(() => {
  cleanup()
})

it("renders an undescribed dialog", () => {
  render(
    <div role="dialog" aria-label="Fixture dialog">
      Missing accessible description
    </div>,
  )
})