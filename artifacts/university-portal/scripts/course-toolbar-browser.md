# Course toolbar browser regression

Run against the already-running University Portal workflow, from the workspace root:

```sh
node artifacts/university-portal/scripts/course-toolbar-browser.mjs
```

Or run `pnpm --filter @workspace/university-portal test:browser:course-toolbar`.
`PORTAL_URL` defaults to `http://localhost:80`; override it to target another
development server. `CHROMIUM_PATH` overrides the Chromium executable (otherwise
resolved from PATH). Uses the existing project's Playwright installation, like
`review-summary-browser.mjs`; no new browser-test dependencies or credentials.

The real `/toolkit` page, CSS, and fonts render in headless Chromium with a
deterministic synthetic course and search options. All API requests are
intercepted; unknown requests and all writes fail the check. No database,
signed-in session, or live catalogue is needed.

At **390, 640, 768, 1024, 1280, 1366 and 1440px**, the script checks actual
bounding boxes, with a 2px rounding allowance:

- From 640px, heading and toolbar start on the same row, without overlap;
  sorting, currency, filter (when shown), and layout controls remain one row.
- On phones, the toolbar sits below the heading/count; controls may wrap
  deliberately, but cannot overlap or escape the header.
- The document does not overflow horizontally at any tested width. Header
  contents must also fit their own container (so clipping cannot hide a failure).
- Currency help is visible initially, hidden after selecting AUD, then restored
  after clearing it. Each state gets the same geometry assertions, plus the
  sort control's help association check.

Exit status is nonzero on failure, with the width, state and measured geometry.
Screenshots for every state and any failure are saved under
`generated-artifacts/course-toolbar/` (already ignored by Git and deployment).
This is a browser geometry check, not a screenshot pixel comparison or a JSDOM
test. Run it alongside the normal DOM tests before shipping toolbar changes.

The run also injects temporary CSS into the test browser to deliberately move
the toolbar below the heading, wrap controls, and overflow the phone viewport.
It must reject all three regressions. The injected styles are removed immediately
and never modify the application source.
