const DIALOG_SELECTOR = '[role="dialog"], [role="alertdialog"]'

function referencedText(element: Element, attribute: string): string {
  return (element.getAttribute(attribute) ?? "")
    .split(/\s+/)
    .filter(Boolean)
    .map((id) => element.ownerDocument.getElementById(id)?.textContent?.trim() ?? "")
    .filter(Boolean)
    .join(" ")
}

function accessibleName(dialog: Element): string {
  return (
    dialog.getAttribute("aria-label")?.trim()
    || referencedText(dialog, "aria-labelledby")
    || ""
  )
}

function accessibleDescription(dialog: Element): string {
  return (
    dialog.getAttribute("aria-description")?.trim()
    || referencedText(dialog, "aria-describedby")
    || ""
  )
}

export function assertOpenDialogsHaveAccessibleContext(
  root: ParentNode = document,
): void {
  root.querySelectorAll(DIALOG_SELECTOR).forEach((dialog, index) => {
    const kind = dialog.getAttribute("role") ?? "dialog"
    const label = `${kind} ${index + 1}`

    if (!accessibleName(dialog)) {
      throw new Error(
        `Opened ${label} has no accessible name. Add DialogTitle, AlertDialogTitle, or aria-label.`,
      )
    }

    if (!accessibleDescription(dialog)) {
      throw new Error(
        `Opened ${label} has no accessible description. Add DialogDescription, AlertDialogDescription, aria-description, or aria-describedby.`,
      )
    }
  })
}