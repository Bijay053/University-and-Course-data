---
name: Prompt assistant scope and grounding
description: User-selected scope and why researched citations use immutable source excerpts.
---

## Scope
The user chose “Scraping and course reviews” for the prompt assistant and said the portal already has the OpenAI API.

**Why:** They want it to find correct information, fix wrong fees/IELTS and missing courses, and avoid unexplained waiting—not a separate general administration chatbot.

**How to apply:** Reuse the existing OpenAI connection and guarded scraper/review operations. New chat requests authorize supported pending-only work automatically; never let generated numbers bypass deterministic source authority or publication approval.

The user said: “do not ask for review and run do it and give me result” and “i do not want manual work.” When reviewing a brief that proposed confirmation before every change, they explicitly chose “Automatically fix pending records.”

**Why:** The user explicitly removed the extra confirmation step for the scrape assistant.

**How to apply:** Automatically fix pending records with clear evidence, using soft exclude with an audit log and undo. Ask for confirmation for published records, bulk changes over 10 rows, or unclear evidence. Honor explicit read-only requests. Permanent deletion and production deployment remain outside automatic pending-record work.

## Provider consolidation holds

An import pause must not prevent ordinary pending-field repairs. Excelsia must
remain paused until its consolidation is reviewed and complete. Resume is an
owner-administrator attestation, not an automated claim that duplicates are gone.

**Why:** The owner required consolidation before allowing further assistant imports,
after repeated fetches created duplicate pending reviews.

**How to apply:** Require an explicit completion confirmation and review note before
resuming. Keep an audit, do not launch old proposals on resume, and retain the
normal publication and large-batch confirmation rules.

## Eligibility policy

A missing CRICOS code means “eligibility unverified”, not automatic rejection. Apply CRICOS checks only to Australian providers. Domestic prices alongside international prices are not exclusion evidence; eligibility must apply to the course itself.

**Why:** The user explicitly settled these rules to avoid excluding valid international courses.

**How to apply:** Keep uncertain records pending and distinguish course-level restrictions from audience-specific fee labels.

## Grounding
Conversation source caches need invalidation when citation ownership or text-cleaning rules change.

**Why:** Corrected extraction code can otherwise keep serving previously cached navigation-heavy evidence.

**How to apply:** Version cached source representations and refresh incompatible entries once; reuse compatible sources normally.

Named existing-course commands such as “add intake Feb and July” edit fields; they must not create another pending course. Explicit “add this course” is a separate intent.

**Why:** The user reported a live intake edit being misrouted to course recovery, leaving the intake empty.

**How to apply:** Resolve existing pending identity first, retain audits, distinguish official verification from staff-provided values, and stop on ambiguous matches.

## Staff-provided edits and provider-wide follow-ups

Direct staff-supplied values must be applied with source “staff-provided” and an audit log, without requiring page evidence. Only flag a supplied value when official evidence contradicts it.

“Add this in all courses” applies the last edit to every course for the current provider, not catalogue discovery. Pending rows can be changed directly; published rows or batches over ten rows require the affected list and confirmation.

**Why:** The user explicitly corrected a refusal to save Sydney and a follow-up that launched discovery instead of applying the edit.

**How to apply:** Retain the last explicit field/value in conversation context, preserve provider scope, keep staff provenance separate from official verification, and never split large edits to bypass confirmation.

Course candidates need course structure: duration, fees, entry requirements or a course code. Exclude news, blogs and events, even when their titles contain degree names.

**Why:** A PhD-graduate celebration article was incorrectly offered as a missing course.

**How to apply:** Require detail-page evidence before presenting an unrecorded link as a course; award words in link text are insufficient.

Official PDF research must remain separate from HTML field extraction and numeric writes.

**Why:** Flattened PDF tables lose the DOM ownership that HTML audience/fee guards rely on. Reusing a general PDF downloader can also bypass the assistant's stricter pinned-host transport. A readable official document proves its text, not that every amount or IELTS score applies to the requested course.

**How to apply:** Reuse parsing primitives, not weaker transport. Keep document/page identity in quotations, report unreadable pages and bounded failures, and treat audience, tuition purpose, year and Annual/Full Course applicability as separate requirements. Continue using validated pending-only correction pipelines rather than assigning generated values.

The user wants this assistant to replace their reliance on Replit for fixes, including software issues, not merely answer scraping questions.

**Why:** They explicitly asked for a capability comparison, scenario checks and a more capable agent after repeated incomplete answers.

**How to apply:** Treat general coding capability as an unmet product goal, not an existing feature. Code editing/testing needs controlled isolated execution; do not give the production chat unrestricted shell access or claim prompt changes supply missing tools.

Ask the model to select immutable excerpt IDs, not to reproduce quotations.

**Why:** In live tests, even an explicit verbatim-quotation instruction produced apparently plausible quotations stitched from nonadjacent table rows and course facts. Strict text validation correctly rejected them but made otherwise useful research fail.

**How to apply:** Generate actual source excerpts server-side, let the model select their IDs, then retrieve and validate the original excerpt. Do not relax validation to accept synthesized extractor summaries as quotations.

## Visible activity and response time
The user requested visible background work and faster answers after seeing only a waiting-worker spinner.

**Why:** A spinner and a changing phase alone do not explain whether research has started, which official pages were checked, or what failed. Long course-scraping jobs must not delay short research requests.

**How to apply:** Show persisted, observed tool activity, failures and real elapsed times—not fabricated progress or private model reasoning. Prefer known official URLs and concurrent independent checks over redundant model planning. Measure queue delay as well as end-to-end time.

Research may run independently of scraping workers, but automatic pending corrections must still use the existing audited worker operations.

**Why:** Low research latency must not bypass source validation, review approval or publication controls. Retries and server restarts can overlap earlier deliveries.

**How to apply:** Bound research concurrency and lifetime. Fence every event and final reply to its current owner; a late old attempt must not overwrite a resumed investigation.

## Verify the requested outcome, not just the research
Check the university's existing course records and other reviews before calling a course missing. Carry the user's verified official course URL into repair validation.

**Why:** ACAP research read the right course while the repair checked unrelated historical pages and failed. The named course also already had an approved record outside the current pending review, so a discovery repair was the wrong conclusion.

**How to apply:** Distinguish pending-only views, existing approved records and genuine discovery gaps. Show actual failure reasons and link to matching newer pending reviews. Do not change unrelated pending courses or rewrite historical failed attempts as successful.

An explicit course URL takes priority over the open review's course. Follow-up phrases such as “this course” and “add this” refer to that user-supplied course, not an unrelated sampled row. An approved record elsewhere is not a reason to refuse a fresh pending review.

Course names have the same targeting priority. Resolve within the selected university, ask the user to choose when several awards match, and keep unresolved names unresolved rather than silently using an open-review row.

**Why:** The user requires name-only requests and corrected course names to work without supplying a URL, while preserving source verification, confirmation and publication controls.

**How to apply:** Retain the chosen course through follow-up questions; a vague reply to a clarification is not permission to select the first option or resume an older course.

**Why:** The user corrected Counselling versus Criminology repeatedly, but the assistant kept answering about the open Criminology review and refusing to add Counselling because it was approved elsewhere.

**How to apply:** Restrict source research and field-edit scope to the requested course. Run a URL-targeted fresh review for “add this”; retain eligibility checks and publication approval. Claim recovery only when the requested course is actually present in the resulting review.

Topic changes end old course references. Review-wide requests, including “all course” and “not only this course,” must stay review-wide through subsequent “do it” or “where is result” messages.

**Why:** The user explicitly changed from Counselling to all unverified courses, but historical URL lookup and stale assistant replies kept restoring Counselling as the subject.

**How to apply:** Fence conversation history at the latest topic change, not just the last URL. Do not use old assistant wording as evidence of the user's current subject. Keep review-wide field corrections separate from missing-course catalogue comparisons.

## Conversation presentation
An explicit request to remove domestic-only courses from a pending review
authorizes evidence-backed exclusion, not merely a research answer. “Check
others as well” includes the review cohort even when a named example appears.

**Why:** The user rejected text-only explanations that acknowledged a
domestic-only record but left it pending.

**How to apply:** Preserve audit records, enforce pending-only and current-owner
write fences, retain uncertain cases, and report saved results. This does not
authorize deleting or unpublishing approved courses.

Confirmed operations must be included in the next answer's context, with their requested URLs and verified results. Approval is not proof that source data is current; allow source-backed corrections through a fresh pending review.

**Why:** The assistant could see conversation prose but not the executed action outcome, repeatedly refused an already-approved course, and left a “no changes applied” research footer above running or completed work.

**How to apply:** Distinguish overall review warnings from IELTS verification. Keep current-review summaries available even when a requested URL is outside that review. Report job completion separately from proven field changes, and exclude editorial/news links from quick catalogue candidates and confirmation.

The user wants the scrape assistant to resemble Replit's live agent chat: no visible 0/2000 limit, and activities and code in the conversation rather than a separate activity panel.

**Why:** The user explicitly requested this presentation instead of the small form and separate activity view.

**How to apply:** Keep observed tool events with their conversation turn, preserve them across subsequent questions, and render actual code or operation details inline. Do not substitute invented commands, fabricated progress, or private reasoning for real activity. Preserve publication approval controls, not extra confirmations for routine pending-only work.
