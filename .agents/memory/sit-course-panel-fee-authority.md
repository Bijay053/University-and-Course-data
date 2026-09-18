---
name: SIT course panel and fee authority
description: Southern Institute of Technology’s safe extraction boundary and international tuition semantics.
---

SIT course pages wrap a small current-programme summary in a very large shared site shell. Treat the current campus, key-information pairs, semester start dates, and application criteria as course-owned; exclude navigation, contact forms, related programmes, module content, and local fee prose.

**Why:** Repeatedly parsing the full page exceeded the per-course deadline, while shared contact text turned “0800 4 0 FEES” into a location and module levels corrupted duration. Semester end dates also looked like intakes.

**How to apply:** Compact before generic extraction. Emit explicit labelled facts and only numbered Semester/Intake start months. Treat SIT course-route case and space encoding variants as one URL identity.

SIT date panels may retain prior-year rolling intakes beside the current year. Keep only start months from the latest explicitly labelled offering year. For merged campus panels, a complete rolling schedule (three or more current-year starts) is authoritative over another campus's shorter semester list; otherwise preserve the union of current starts.

**Why:** Hotel Management's Queenstown panel showed two 2026 starts followed by five 2027 starts, while Invercargill added two semester months. Flattening every panel produced seven unqualified months instead of the five current rolling intakes.

**How to apply:** Carry an explicit year across subsequent unlabelled Semester/Intake entries, discard older labelled years, and select the longest complete rolling schedule only when one exists. Never collect end-date months.

An empty base summary may be recovered only when the exact programme (including approved aliases) has usable tuition in the populated international schedule. Follow only same-host child paths under that exact programme's `/campus/` route, cap and deadline the fetches, and require each child to repeat the exact programme name and a valid current panel. If physical/hybrid and online siblings coexist, use the physical/hybrid panels; normalize Hyflex as delivery rather than a campus. Preserve the full-time duration and remove only explicit part-time maxima.

**Why:** Some current multi-campus programmes render facts only on campus child routes, but obsolete and domestic shells expose the same link pattern. Following every shell would create false international records; merging online siblings contaminates otherwise eligible physical offerings; broad duration cleanup can erase legitimate “Up to … full-time” values.

**How to apply:** A schedule outage defers recovery as retryable. An unmatched shell remains excluded without child fetches. Failed or identity-mismatched child fetches fall back to the title-only skip. Never infer eligibility from child-panel existence alone.

SIT’s central international schedule is the tuition authority. Use its Tuition Fee column, not Resource Fee or Total Fee, force NZD, and require exact award-name matching.

**Why:** Course pages advertise domestic Zero Fees and direct material costs. The schedule’s total combines tuition and resource costs, while fuzzy matching can map certificates to diplomas or unrelated awards.

**How to apply:** Central tuition may override local amounts only after an exact programme match with a non-empty tuition value. SIT's runtime config identity is `sit`, not its long institution-name slug; verify the production loader call when naming ID-specific YAML. A required YAML schedule replaces stale legacy and request-level fee-page values. Central prefetch must fall back to the orchestrator's already-loaded config when task-local context is empty. A course absent from the populated international schedule is not eligible for staging; a schedule fetch failure remains retryable.