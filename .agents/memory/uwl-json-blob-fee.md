---
name: UWL international fee — read the SSR JSON blob, not the rendered <select>
description: On static (render=false) UWL pages the nationality <select> is empty; the int fee lives only in the Angular SSR JSON blob
---

# UWL international fee: trust the SSR JSON blob, not the JS <select>

UWL (University of West London) course pages are an Angular SPA that is also
server-side rendered. After the cost optimisation switched UWL to Scrape.do
`render=false` (static SSR), the nationality-switcher `<select>` is fetched as
an **empty/partial shell** — its option text (`"£16,750 – International"` /
`"£9,790 – UK"`) is populated client-side and never appears in static HTML.

**Trap:** a select-based fee reader on static HTML sees only a UK option (or no
options) and returns a **false domestic-only verdict**, so every UWL course
loses its international fee even when it clearly has one.

**Authoritative source:** the Angular SSR data blob embedded in the page —
present in BOTH static and headless HTML — carries the real fees as
`field_p_cv_int_main_fee` (international) and `field_p_cv_uk_eu_main_fee`
(UK/EU), each shaped `"field_…":{"target_id":N,"name":"<digits>"}`.

**Rule:** read the JSON blob FIRST, before any JS-rendered widget. If
`int_main_fee` is present → that is the international fee. If only
`uk_eu_main_fee` is present → genuine domestic-only course (no intl fee). The
`<select>` path is now only a legacy fallback for pages with no blob.

**Operator policy this enforces:** *if a course is offered to international
students it always has an international fee* — so any present `int_main_fee`
must be captured, never dropped.

**Why:** the cost-optimisation render=false switch silently broke fee
extraction because the prior reader depended on JS-rendered option text. Any
future "save credits by switching to static" change must re-verify that the
fields it reads exist in the *static* HTML, not just the headless render.

**How to apply:** pick the FIRST `int_main_fee` occurrence — it is the headline
full-time fee; later occurrences belong to linked/related courses embedded on
the same page. Host-gate the whole thing to `*.uwl.ac.uk`. See
`test_fee_uwl_json_blob_*` in `tests/test_extractors.py`.
