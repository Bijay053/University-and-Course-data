"""Observed page coverage, independent of the model's prose or conclusions."""


def research_coverage(rows, sources):
    def key(value):
        return str(value or "").rstrip("/")

    checked = {key(source["url"]): source for source in sources}
    counts = {"total": len(rows), "checked": 0, "failed": 0, "unchecked": 0}
    items = []
    for row in rows:
        source = checked.get(key(row.get("url")))
        state = "unchecked" if source is None else ("checked" if source.get("verified") else "failed")
        counts[state] += 1
        items.append({"id": row["id"], "name": row["name"], "url": row.get("url"), "pageStatus": state})
    summary = (
        f"Course-page coverage: {counts['checked']}/{counts['total']} read successfully, "
        f"{counts['failed']} unavailable, {counts['unchecked']} not checked."
    )
    return {**counts, "rows": items, "summary": summary}
