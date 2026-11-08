"""Bounded fallbacks through the normal permission-checked source reader."""
from urllib.parse import urlsplit


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.netloc.lower()


async def read_alternatives(primary, candidates, reader):
    checks = []
    # An unavailable robots policy affects every path on this exact origin.
    # A Disallow is path-specific and must not suppress an allowed alternative.
    unavailable = set()
    if primary.get("accessIssue") == "robots_unavailable":
        unavailable.add(origin(primary["url"]))
    seen = {primary["url"]}
    for candidate in candidates[:5]:
        if candidate in seen:
            continue
        seen.add(candidate)
        if origin(candidate) in unavailable:
            checks.append({"url": candidate, "verified": False,
                           "reason": "Not requested: this origin's robots.txt could not be verified."})
            continue
        checked = await reader(candidate)
        checks.append({"url": candidate, "verified": checked.get("verified") is True,
                       "reason": checked.get("error", "")})
        if checked.get("verified"):
            return {**checked, "_requestedDocumentUrl": primary["url"],
                    "_alternativeChecks": checks}
        if checked.get("accessIssue") == "robots_unavailable":
            unavailable.add(origin(candidate))
    return {**primary, "_alternativeChecks": checks}
