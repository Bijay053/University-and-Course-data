import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";
import { startFrontendReleaseGuard } from "./lib/frontend-release-guard";

// Embedded in the production bundle so guarded releases can prove that the
// public asset came from the target Git revision.
const releaseMarker = import.meta.env.VITE_RELEASE_MARKER;
if (releaseMarker) {
  document.documentElement.dataset.releaseMarker = releaseMarker;
}

startFrontendReleaseGuard();

// Prevent the browser's default behaviour of changing a focused
// number input's value when the user scrolls the mouse wheel over it.
// Without this, scrolling down the page after typing a value silently
// edits whatever number input still holds focus (Reading score, fee,
// duration, etc.) — which the user never asked for.
//
// We blur the input on wheel so the page scroll proceeds normally.
window.addEventListener(
  "wheel",
  (e) => {
    const t = e.target as HTMLElement | null;
    if (
      t &&
      t.tagName === "INPUT" &&
      (t as HTMLInputElement).type === "number" &&
      document.activeElement === t
    ) {
      (t as HTMLInputElement).blur();
    }
  },
  { passive: true },
);

createRoot(document.getElementById("root")!).render(<App />);
