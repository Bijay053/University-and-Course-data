"""Follow direct official redirects, with a fresh permission check at each hop."""
from urllib.parse import urlsplit

MAX_REDIRECTS = 3


async def read_official_source(url, home, request, *, redirects=None):
    from app.services.scrape_assistant_tools import SourceReadError, official_url

    current = official_url(url, home)
    seen, chain = {current}, redirects if redirects is not None else []
    for hop in range(MAX_REDIRECTS + 1):
        try:
            return await request(current), current, chain
        except SourceReadError as exc:
            target = getattr(exc, "redirect_url", None)
            # Provider redirects do not establish university ownership.
            if not target:
                raise
            try:
                target = official_url(target, home)
            except (ValueError, TypeError):
                raise SourceReadError(
                    "Official source redirected outside the permitted HTTPS university website; "
                    "the destination was not requested.",
                    failure_stage="source_redirect",
                ) from None
            if target in seen:
                raise SourceReadError("Official source has a redirect loop; no repair was queued.",
                                      failure_stage="source_redirect") from None
            if hop == MAX_REDIRECTS:
                raise SourceReadError("Official source exceeded the safe redirect limit.",
                                      failure_stage="source_redirect") from None
            chain.append({"from": current, "to": target})
            seen.add(target)
            # request() revalidates robots and pins DNS at every new destination.
            current = target


def same_path_redirect(chain):
    """A default offering query or slash change retains the original page identity.

    A redirect to another course, a homepage or a sign-in page must not make that
    page's facts into evidence for the originally requested course.
    """
    return bool(chain) and all(
        urlsplit(hop["from"]).path.rstrip("/") == urlsplit(hop["to"]).path.rstrip("/")
        for hop in chain
    )
