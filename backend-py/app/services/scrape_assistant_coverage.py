"""Observed page coverage, independent of the model's prose or conclusions."""
from urllib.parse import urlsplit


def research_coverage(rows, sources):
    def key(value):
        return urlsplit(str(value or ""))._replace(fragment="").geturl().rstrip("/")

    checked = {key(source["url"]): source for source in sources if source.get("url")}
    counts = {"total": len(rows), "checked": 0, "failed": 0, "unchecked": 0}
    items = []
    for row in rows:
        source = checked.get(key(row.get("url")))
        state = "unchecked" if source is None else ("checked" if source.get("verified") else "failed")
        counts[state] += 1
        items.append({"id": row["id"], "name": row["name"], "url": row.get("url"), "pageStatus": state})
    course_summary = (
        f"Course-page coverage: {counts['checked']}/{counts['total']} read successfully, "
        f"{counts['failed']} unavailable, {counts['unchecked']} not checked."
    )
    # A central PDF is evidence for applicability research, not a course page.
    # Report its attempted read even when the requested URL matches no row.
    source_items = []
    for source in checked.values():
        source_items.append({
            "url": source["url"],
            "sourceType": source.get("sourceType") or (
                "pdf" if urlsplit(source["url"]).path.lower().endswith(".pdf") else "html"
            ),
            "pageStatus": "checked" if source.get("verified") else "failed",
            **({"error": source["error"]} if source.get("error") else {}),
        })

    def totals(entries):
        read = sum(item["pageStatus"] == "checked" for item in entries)
        return {"total": len(entries), "checked": read, "failed": len(entries) - read}

    source_counts = totals(source_items)
    pdf_counts = totals([item for item in source_items if item["sourceType"] == "pdf"])
    summaries = [course_summary] if rows else []
    if source_items:
        summaries.append(
            f"Source checks: {source_counts['checked']}/{source_counts['total']} read successfully, "
            f"{source_counts['failed']} unavailable."
        )
    if pdf_counts["total"]:
        summaries.append(
            f"PDF checks: {pdf_counts['checked']}/{pdf_counts['total']} read successfully, "
            f"{pdf_counts['failed']} unavailable. "
            "Reading central guidance does not establish its applicability to every course."
        )
    return {
        **counts, "rows": items,
        "sourceChecks": {**source_counts, "rows": source_items, "pdf": pdf_counts},
        "summary": " ".join(summaries) or "No source checks were requested.",
    }
