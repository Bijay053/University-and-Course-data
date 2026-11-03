---
name: Public course toolkit
description: Visitor access and scope requested for the Study Info Centre course browser.
---

The user wants a public toolkit like meatoolkit.com/degrees where any visitor
can search courses and see fees without signing in. This is part of the existing
Study Info Centre product, not a separate university-data product.

**Why:** The user explicitly requested a public page accessible to any user.

**How to apply:** Keep visitor browsing separate from protected administration.
Do not expose pending scraper/reviewer data. Display original fee currency,
period and year without inventing exchange rates or missing prices.

The toolkit should match the existing landing/sign-in page's fonts and colours,
rather than use a separate visual identity.

**Why:** The user requested matching fonts and colours after viewing the toolkit.

**How to apply:** Reuse the portal's shared typography and brand colours when
styling public course browsing. Show a small Beta label beside the toolkit
branding with a blinking red background and white text, as explicitly requested
by the user. Respect reduced-motion preferences.

Visitors must be able to report missing or incorrect course information and
university Key Insights using a short form, and staff must see the submissions
inside the dashboard. Key Insights reports must identify the university and
the relevant insight country, not an unrelated course.
Staff need direct navigation from dashboard reports to the affected course and
university so they can investigate the reported information.

**Why:** The user explicitly requested public error reports and dashboard review.

**How to apply:** Keep reports private and review-only. Never treat anonymous
reports as scraper learning, source authority, or instructions to change course
data automatically; they are unverified until staff investigate them.

Place Course name and Location fields directly above the course cards.

**Why:** After requesting these result-area fields, the user explicitly requested
removing the duplicate sidebar course search and Destination country filter.

**How to apply:** Keep these two fields above results on desktop and mobile.
Do not restore the duplicate sidebar search. The user also requested removing
University and Degree level from this sidebar. University-directory Country and
City controls are a separate scope.

The user subsequently requested the advanced filter options from the existing
staff `/search` page in the public toolkit, superseding the earlier removal of
Destination country.

**Why:** The user explicitly linked `/search` and asked to use its filters as-is.

**How to apply:** Preserve destination, applicant qualification/grading, English
overall/bands, other exams, intakes, duration and tuition controls. Keep fee
comparisons currency-specific; no silent conversion. No University or Degree
level control is required by that reference.

Toolkit filter dropdowns must support typing within a single control.

**Why:** The user requested searchable dropdowns and questioned the duplicated
university search box plus select.

**How to apply:** Use one searchable selection control per filter, not a separate
search input above a native dropdown.

Intakes must use a dropdown with multiple month selection, not a month-button grid.

**Why:** The user explicitly requested intake options in a dropdown with
multi-selection.

**How to apply:** Preserve selections while searching the list and allow removing
individual months or clearing all selected intakes.

Course-name search is name-only; location means the course's own campus.

**Why:** The user reported irrelevant courses for “bachelor of information
technology” with “melborne”; shared award words and a university's home city
must not qualify a differently named course or a course at another campus.

**How to apply:** Tolerate spelling mistakes while requiring every meaningful
name term. Prefer explicit course/offerings locations over university metadata,
keep budget and location on the same offering, and display the matched campus
and matched fee. Preserve complete source offerings for full course details.

Currency selection must be easy to find beside fee sorting.

**Why:** The user reported fee sorting as broken when its options were disabled
until choosing a currency far down the filter sidebar.

**How to apply:** Keep currency-specific comparisons, but provide an explicit
currency choice by the sort control and synchronize it with the fee-range filter.
The user wants sort, currency and layout controls to the right of the Courses
heading on desktop, not wrapped beneath it; allow wrapping on narrow phones.

Verify dropdown visibility, not only whether a browser can click an option.

**Why:** An overflow-hidden accordion clipped option lists, but automated clicks
could still succeed by scrolling the clipped content and hiding the trigger.

**How to apply:** Check the open trigger and option list together in desktop and
mobile filters; option hit-testing and screenshots catch clipping that DOM tests miss.

Visitors opening the website's main link should land on the public toolkit,
not the staff sign-in page.

**Why:** The user explicitly requested the toolkit when opening the website.

**How to apply:** Keep anonymous home navigation public while preserving staff
sign-in and protected administration.

University Key Insights are intended to be readable by public toolkit visitors.

**Why:** The user explicitly requested each university's saved Key Insights in
the toolkit so visitors can see them.

**How to apply:** Preserve country-specific context and original guidance,
keep editing staff-only, and do not trigger AI parsing or writes on public reads.

Hide missing public course details and empty Key Insights rather than showing
"Not published", blank rows, or a no-insights placeholder. Hide the university's
Key Insights tab too when there are no saved insights.

**Why:** The user explicitly requested hiding unavailable information after
seeing the empty Field row and Key Insights box.

**How to apply:** Omit unavailable public display fields while keeping real
values visible; do not confuse a failed request with confirmed absent data.

University search must be a separate, prominent mode alongside course search,
not only a university filter in the course sidebar.

**Why:** The user clarified that the sidebar search did not satisfy the requested
separate university search option.

**How to apply:** Preserve distinct Courses and Universities browsing, with
university results opening a full public university page with Courses and
Key Insights tabs, rather than a side panel.
Place the university-name search inside the Where to study filter panel above
Country and City, not in the hero header, as requested by the user.

Public Key Insights must match the existing admin card design.

For course browsing, keep Key Insights on the full course page, not in the
compact course preview/card panel. The full university Insights tab remains.

**Why:** The user explicitly requested removing insights from the preview panel
and showing them on the full course page.

**How to apply:** Keep preview details compact; retain guidance on full pages.

Full public course pages should include saved course scholarships.

**Why:** The user explicitly flagged missing Scholarships on the course page.

**How to apply:** Show available course-owned scholarship names, values and
eligibility without inventing discounts or currencies. Keep university-level
scholarship guidance in its existing Key Insights context.

**Why:** The user explicitly requested full university pages and the same
Key Insights design shown when staff add insights.

**How to apply:** Reuse the read-only card presentation, including country
filters and structured fields, without exposing administration controls.

## Comparison download consistency

Downloads should represent the comparison the visitor is currently viewing,
not a silently refreshed or recalculated version.

**Why:** A saved comparison needs to match the values the visitor just reviewed,
especially fees with different currencies, periods and publication years.

**How to apply:** Preserve the displayed course selection and published fee
semantics. Do not quietly re-fetch or convert amounts during export; explicitly
identify omitted unavailable selections.

## University search focus

Keep a single focus indicator around the rounded university-name field, not
an additional rectangular outline around the field and its label.

**Why:** The user repeatedly flagged boxes appearing when clicking search.
A form-level focus style surrounds the label too after search moves into the sidebar.

**How to apply:** Preserve visible mouse and keyboard focus on the field itself
when moving or restyling search controls; do not outline their enclosing form.
