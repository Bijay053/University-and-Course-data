"""Public projection of cited, unresolved duration review metadata."""
from app.services.scraper.extractors.ulaw_sqe2_demands import REVIEW_KEY


def public_duration_review_status(row):
    if getattr(row, "duration", None) is not None:
        return None
    metadata = getattr(row, "extraction_method", None)
    review = metadata.get(REVIEW_KEY) if isinstance(metadata, dict) else None
    if not isinstance(review, dict) or review.get("status") != "confirmed_unpublished":
        return None
    sources = review.get("sources")
    if not isinstance(sources, list) or not sources or not all(
        isinstance(source, dict)
        and isinstance(source.get("url"), str) and source["url"]
        and isinstance(source.get("snippet"), str) and source["snippet"]
        for source in sources
    ):
        return None
    if not isinstance(review.get("reason"), str) or not review["reason"]:
        return None
    # A changed course URL invalidates the old review proof.
    if not any(source["url"] == getattr(row, "course_website", None) for source in sources):
        return None
    return {
        "status": "confirmed_unpublished", "reason": review["reason"],
        "sources": [{"url": s["url"], "snippet": s["snippet"]} for s in sources],
    }