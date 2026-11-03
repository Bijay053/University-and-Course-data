"""Build the offline review deliverables. No database, network, or application writes."""
from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(name):
    return json.loads((ROOT / name).read_text())


def clean(value):
    return re.sub(r"\s+", " ", value.replace("*", "").replace("_", "")).strip()


def review(record, staged=False):
    ident = record["id"]
    prefix = "staged-" if staged else ""
    page = load(f"evidence/{prefix}{ident}.json")
    excerpts = page.get("evidence", []) + page.get("additional_evidence", [])
    proposal = None
    delivery = None
    load_label = None
    quote = ""
    note = "Study load is not proof of delivery; leave delivery_mode, eligibility and approvals unchanged."
    status = "supported"
    label = next(
        (re.search(r"Study mode:?\s*\n+\s*([^\n]+)", e["context"], re.I)
         for e in excerpts
         if re.search(r"Study mode:?\s*\n+\s*([^\n]+)", e["context"], re.I)),
        None,
    )
    durations = list(dict.fromkeys(
        match for e in excerpts
        for match in re.findall(r"\*\*Duration:\*\*[^\n]+", e["context"])
    ))
    if page["characters"] < 100:
        quote = "Stored official URL returns a 404 page; independently confirmed HTTP 404."
        note = "No current course-owned delivery evidence. Unknown; do not infer from historical text or another course."
    elif not staged and ident in {14484, 14518}:
        quote = clean(durations[0])
        note = "Duration has no study-load label. Full-time fee wording and lectures/labs alone do not establish the course's complete mode options. Unknown."
    elif (not staged and ident == 14555) or (staged and ident == 142694):
        quote = "Study Duration: 3 years flexible online learning. Study anytime, from anywhere."
        note = "Stored top-up URL redirects to the full three-year degree. Evidence belongs to the broader award, not a verified top-up route; unknown for this record."
    elif (not staged and ident in {14554, 14593, 14602, 14633, 14634}) or (staged and ident == 142704):
        proposal = delivery = "Online"
        duration = "12 months" if ident in {14633, 142704} else "3 years"
        quote = f"Study Duration: {duration} flexible online learning. Study anytime, from anywhere."
        note = "Course-owned Study Duration and How Will You Study Online sections on the partner page reached from DMU; not inferred from title. No study-load or eligibility correction proposed."
    elif staged and ident == 142842:
        proposal = load_label = "Part-time"
        quote = "Duration: Part-time. Location of study: De Montfort University."
        note = "Rejected historical CPD row, with old 2024/25 dates. Part-time supported, delivery not established. Preserve rejection and non-international restriction; no reinstatement proposed."
    elif label:
        proposal = clean(label.group(1))
        quote = "Study mode: " + proposal
        if "Full-time" in proposal:
            load_label = "Full-time, Part-time" if "Part-time" in proposal else "Full-time"
        elif "Part-time" in proposal:
            load_label = "Part-time"
        if ident == 14529:
            delivery = "Online (distance learning)"
            note = "Course teaching section specifies online lectures and virtual-learning content. Part-time-only evidence is not permission to change eligibility or approvals."
        if ident == 14540:
            proposal = "Part-time, Distance learning (limited campus attendance)"
            delivery = "Distance learning with limited campus attendance"
            quote += ". Teaching: distance learning approach with minimal on-campus learning. PG Cert induction and study days delivered on campus; PG Dip distance learning, online or on campus depending upon module choice."
            note = "Preserve the detailed mixed-delivery facts. Do not simplify this to exclusively Online or infer full-time eligibility."
    elif ident == 14490:
        proposal = "Part-time, Online"
        load_label = "Part-time"
        delivery = "Online"
        quote = "Duration: Two years part-time. Location of study: Online learning."
        note = "Labelled course facts in both audience panels; not generic online mentions. No eligibility or approval changes."
    elif durations:
        quote = " / ".join(clean(d) for d in durations)
        proposal = load_label = "Full-time"
        if ident in {14473, 14499, 14521, 14541}:
            proposal = load_label = "Full-time, Part-time"
        if ident == 14473:
            delivery = "On Campus"
            quote += ". Attendance: You can expect to study on campus one day a week, with occasional classes on a second day."
        if ident == 14513:
            delivery = "On Campus"
            quote += ". Attendance: on campus."
        if ident == 14509:
            delivery = "On Campus"
            quote += ". International Attendance: Full-time students can expect to attend up to three days of classes at the university a week."
        if ident in {14521, 14541}:
            note += " Full-time and part-time are advertised in the UK panel; international panel supports full-time only. Retain audience distinction; part-time is not verified international availability."
        if ident == 14499:
            note += " Page says part-time entry for September 2026 is no longer accepting applications; advertised load options do not prove a currently open intake."
    if proposal is None:
        status = "unknown"
    return {
        "table": "scraped_courses" if staged else "courses",
        "id": ident,
        "course_id": record.get("course_id") if staged else ident,
        "name": record["name"],
        "university": record["university"],
        "current_study_mode": record["study_mode"],
        "classification": "confirmed invalid",
        "reason": (
            "Section heading, not a study-mode value"
            if record["study_mode"] in {"Key features", "Personal Statement selection criteria"}
            else "Accreditation disclaimer, not a study-mode value"
            if record["study_mode"].startswith("*This accreditation")
            else "Narrative course/curriculum/career prose, not a concise delivery/load label"
        ),
        "stored_official_url": record["course_website"],
        "checked_at": page["checked_at"],
        "source_page_title": clean(page["title"]),
        "evidence_quote": quote,
        "proposed_study_mode": proposal,
        "verified_study_load": load_label,
        "verified_delivery": delivery,
        "proposal_status": status,
        "notes": note,
        "record_status": record["status"],
        "approval_status": record.get("approval_status"),
        "evidence_file": f"evidence/{prefix}{ident}.json",
    }


def main():
    affected = load("development-invalid-inventory.json")
    staged = load("staged-invalid-inventory.json")
    ambiguous = load("ambiguous-labels.json")
    primary = [review(r) for r in affected]
    by_url = {r["stored_official_url"]: r for r in primary}
    staging = []
    for r in staged:
        if r["course_website"] in by_url:
            result = dict(by_url[r["course_website"]])
            result.update(table="scraped_courses", id=r["id"], course_id=r["course_id"],
                          current_study_mode=r["study_mode"], record_status=r["status"],
                          approval_status=None)
            result["notes"] += " Historical staging copy: do not reapprove, reopen or overwrite automatically."
        else:
            result = review(r, staged=True)
        staging.append(result)
    all_rows = primary + staging
    for name, rows in [("course-review", primary), ("staging-review", staging)]:
        (ROOT / f"{name}.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
        with (ROOT / f"{name}.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    counts = Counter(r["proposal_status"] for r in primary)
    summary = f"""# Development study-mode review

Read-only inventory and official-page review, 3 November 2026 (UTC).

## Results
- All **11,965** development `courses` records inventoried, including every distinct non-null study-mode value (not just values over a length threshold).
- **64 confirmed invalid values**, all at De Montfort University: **{counts['supported']} supported replacement proposals**, **{counts['unknown']} unknown replacements**.
- **13 ambiguous labels**: 11 `Both`, 2 `Mixed`. Not confirmed invalid and not proposed for correction.
- **11,286 legitimate delivery/load labels by field meaning**; **602 nulls**, no empty strings. This is semantic triage, not independent re-verification of every otherwise valid record.
- Supplementary inventory of all **36,221 staged rows**: **67 invalid**, 13 ambiguous, 34,009 legitimate labels, 2,132 nulls; no empty strings. Invalid staging rows: 62 approved historical copies, 5 rejected. All 67 included separately, including three additional stored URLs, each inspected.

## Boundaries and interpretation
No course data, staging data, approvals, eligibility, application code or production records changed. Only development SELECT queries and public official-page requests were used. No re-scrapes, repairs, imports or approval endpoints invoked.

`Full-time` / `Part-time` are legitimate study-load labels in this existing field, not delivery proof. Explicit `On Campus, Online`, `Online, On Campus`, `Full-time, Part-time`, and `Blended` values are not invalid merely because they contain alternatives. `Both`/`Mixed` may represent real alternatives but do not identify them; preserved as ambiguous. Nulls are missing, not confirmed corrupt values.

Proposals reproduce labelled course Study mode facts, explicitly labelled course Duration/load facts, or course-owned delivery/attendance sections. Generic scholarships, global opportunities, open days, facilities, online events, course titles and navigation are not delivery evidence. Course-owned teaching delivery was inspected but not used to guess On Campus from lectures/laboratories alone. Unknown delivery remains null even when Full-time study load is verified.

For unknown replacement proposals, **no executable replacement is proposed**. If the user later approves removing invalid prose, blank/null may represent unknown, but this review does not authorize clearing it. The original values remain in the database and in the exports.

Historical snapshots/backups are not current course records and are not cleanup targets. Staging copies are context only; their existing approval/rejection status must not change.

## Important cases
- **14671 Accounting Top Up BSc (Hons)**: labelled Study mode **Full-time** (also repeated in full Key facts). Proposed study_mode `Full-time`; delivery remains unknown in this proposal. No inference from generic campus or online training text.
- **14540 Clinical Pharmacy**: `Part-time, Distance learning (limited campus attendance)`; preserve optional/recommended campus days and module-dependent delivery, rather than flattening to exclusively Online.
- **14529 Cricket Management**: labelled `Part-time, Distance learning`, with course-specific online lectures.
- **14470, 14472, 14531, 14574**: stored URLs HTTP 404; replacement unknown.
- **14484, 14518**: duration alone does not state load; full-time fee wording insufficient to establish complete mode options; replacement unknown.
- **14555**: former Retail Design top-up URL redirects to full three-year degree; do not transfer its delivery proof to an unverified award variant. Same caution for staged **142694** (Heritage Interior Design top-up).
- **14521, 14541**: UK panels advertise part-time alternatives; international panels support full-time only. Proposed combined field label is not international eligibility approval.
- Additional rejected staged **142842**: historical Nursing Associates CPD page supports Part-time, but must stay rejected; no international/degree reinstatement proposed.

## Review files
- `course-review.csv` / `.json`: every affected published-course ID, full original value, exact stored URL, checked time, evidence excerpt, proposed value, unknown delivery/load fields and caveats.
- `staging-review.csv` / `.json`: all 67 staged copies, with table-specific IDs and course links. Do not confuse staged IDs with course IDs.
- `ambiguous-labels.json`: all 13 ambiguous current-course records; preserve, no correction proposed.
- `distinct-values.csv`, `staged-distinct-values.csv`: complete distinct-value inventory, including legitimate labels.
- `development-invalid-inventory.json`, `staged-invalid-inventory.json`: original read-only record snapshots.
- `evidence/*.json`: page titles, retrieval times and course-page excerpts. Keyword matches are retained as review context, **not automatically accepted evidence**; the reviewed quote and decision are in the review files.
- `transport-checks.json`: independent HTTP status/redirect checks for unavailable and partner URLs. The content-fetch service reports the requested URL even after redirect, so use the transport checks for final destinations.
- `inventory.sql`: read-only inventory queries; no mutation or correction SQL supplied.
- `validation.json`: export consistency and unchanged-record comparison.

## Approval needed before any correction
Review the exact table/ID/current-value/proposed-value pairs and caveats. A later approved change must re-read and compare current values, updated timestamps and course identity against this snapshot, reject drift, preserve evidence and approvals, and remain development-only unless production changes are separately authorized. Do not bulk-normalize legitimate labels or replace unknowns with a campus default.
"""
    (ROOT / "README.md").write_text(summary)
    esc = lambda x: html.escape("" if x is None else str(x))
    sections = []
    for title, rows in [("Current course records — 64 confirmed invalid", primary),
                        ("Staging context — 67 historical rows, not approval actions", staging)]:
        cards = []
        for r in rows:
            proposal = r["proposed_study_mode"] or "UNKNOWN — no replacement proposed"
            cards.append(f"""<article><h3>{esc(r['table'])} #{r['id']} · {esc(r['name'])}</h3>
<p><b>Current:</b> {esc(r['current_study_mode'])}</p><p><b>Reason:</b> {esc(r['reason'])}</p>
<p><b>Proposed:</b> {esc(proposal)} · <b>Delivery:</b> {esc(r['verified_delivery'] or 'Unknown')} · <b>Load:</b> {esc(r['verified_study_load'] or 'Unknown')}</p>
<blockquote>{esc(r['evidence_quote'])}</blockquote>
<p><a href="{esc(r['stored_official_url'])}">Stored official course URL</a> · {esc(r['source_page_title'])} · checked {esc(r['checked_at'])}</p>
<p>{esc(r['notes'])}</p><small>University: {esc(r['university'])} · Status: {esc(r['record_status'])} · Approval: {esc(r['approval_status'] or 'see staged status')} · Linked course ID: {esc(r['course_id'])}</small></article>""")
        sections.append(f"<h2>{esc(title)}</h2>" + "".join(cards))
    (ROOT / "review.html").write_text(f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Development study-mode review</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:1050px;margin:32px auto;padding:0 20px;color:#172338;background:#f4f6f8}}h1,h2{{line-height:1.2}}article{{background:white;border:1px solid #ccd3db;border-radius:8px;padding:20px;margin:18px 0;overflow-wrap:anywhere}}blockquote{{border-left:4px solid #25737e;margin:16px 0;padding:8px 16px;background:#eef7f6}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#075cab}}small{{color:#526074}}.notice{{background:#fff2ce;padding:18px;border:1px solid #dbc276}}</style>
<h1>Development study-mode review</h1><p>3 November 2026 · Read-only · Explicit approval required before any correction</p>
<div class="notice"><b>No records changed.</b> 64 invalid current-course values: {counts['supported']} supported proposals and {counts['unknown']} unknown replacements. 13 ambiguous labels preserved separately. Staging entries below are historical context, not approval requests.</div>
<details><summary>Scope, methodology, exceptions and approval safeguards</summary><pre>{esc(summary)}</pre></details>
{''.join(sections)}<h2>Ambiguous — not confirmed invalid; no correction proposed</h2>
{''.join(f"<article><b>courses #{r['id']} · {esc(r['name'])}</b><p>{esc(r['university'])} · Current: {esc(r['study_mode'])}</p><a href='{esc(r['course_website'])}'>Stored official URL</a><p>Could describe legitimate alternatives; not independently verified in this invalid-value review. Preserve unchanged.</p></article>" for r in ambiguous)}
</html>""")
    assert len(primary) == 64 and len(staging) == 67 and len(ambiguous) == 13
    assert len({r["id"] for r in primary}) == 64
    assert all(r["evidence_quote"] and r["checked_at"] for r in all_rows)
    assert next(r for r in primary if r["id"] == 14671)["proposed_study_mode"] == "Full-time"
    assert all(r["proposed_study_mode"] is None for r in primary if r["proposal_status"] == "unknown")
    print(json.dumps({"courses": len(primary), "staged": len(staging), "proposals": dict(counts)}))


if __name__ == "__main__":
    main()
