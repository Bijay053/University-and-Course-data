// @vitest-environment jsdom

import { afterEach, describe, expect, it } from "vitest"

import { assertOpenDialogsHaveAccessibleContext } from "./dialog-accessibility"

afterEach(() => {
  document.body.replaceChildren()
})

function renderDialog(attributes: string, context = ""): void {
  document.body.innerHTML = `${context}<div role="dialog" ${attributes}></div>`
}

describe("portal dialog accessibility guard", () => {
  it("rejects an opened dialog without an accessible name", () => {
    renderDialog('aria-describedby="description"', '<p id="description">Confirm the action.</p>')

    expect(() => assertOpenDialogsHaveAccessibleContext()).toThrow(
      "Opened dialog 1 has no accessible name",
    )
    document.body.replaceChildren()
  })

  it("rejects an opened dialog without an accessible description", () => {
    renderDialog('aria-labelledby="title"', '<h2 id="title">Confirm action</h2>')

    expect(() => assertOpenDialogsHaveAccessibleContext()).toThrow(
      "Opened dialog 1 has no accessible description",
    )
    document.body.replaceChildren()
  })

  it("accepts an opened dialog with meaningful labelled context", () => {
    renderDialog(
      'aria-labelledby="title" aria-describedby="description"',
      '<h2 id="title">Confirm action</h2><p id="description">Review the changes before continuing.</p>',
    )

    expect(() => assertOpenDialogsHaveAccessibleContext()).not.toThrow()
  })
})