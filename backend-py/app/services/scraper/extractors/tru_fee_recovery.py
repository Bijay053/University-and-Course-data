"""Bounded TRU calculator recovery through the normal source-access controls."""
import asyncio
import re
from urllib.parse import urlsplit, urljoin


def publisher_move(raw, url):
    """Only a tiny, explicit TRU move to the same programme's static page."""
    if len(raw) > 6000:
        return None
    from bs4 import BeautifulSoup
    from app.services.scrape_assistant_tools import official_url
    soup = BeautifulSoup(raw, "html.parser")
    tags = [t for t in soup.select("meta[http-equiv]")
            if str(t.get("http-equiv", "")).lower() == "refresh"]
    if len(tags) != 1:
        return None
    match = re.fullmatch(r"\s*[01]\s*;\s*url\s*=\s*([^\s\"'<>]+)\s*",
                         tags[0].get("content", ""), re.I)
    if not match:
        return None
    target = official_url(urljoin(url, match[1]), url)
    source_path = urlsplit(url).path.rstrip("/")
    target_path = urlsplit(target).path
    return target if target_path.endswith(".html") and target_path[:-5] == source_path else None


async def recover(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.tru.ca", "tru.ca"}
            or not parsed.path.startswith(("/programs/catalogue/", "/distance/programs/"))):
        return None
    from app.services.official_source_redirects import read_official_source
    from app.services.scrape_assistant_tools import public_request
    from .tru_calculator import extract
    try:
        raw, final, _ = await asyncio.wait_for(
            read_official_source(url, url, public_request), timeout=15)
        moved = publisher_move(raw, final)
        if moved:
            raw, final, _ = await asyncio.wait_for(
                read_official_source(moved, url, public_request), timeout=15)
        if parsed.path.startswith("/distance/programs/"):
            from .tru_open_learning_fees import extract as open_learning_fee
            fee = await open_learning_fee(raw.decode("utf-8", errors="strict"), final)
            if fee is None:
                return {"_official_course_url": final} if moved else None
            known = True
        else:
            known, fee = await extract(raw.decode("utf-8", errors="strict"), final)
        if not known:
            return None
        if fee is None:
            return {"status": "failed", "error": "TRU international calculator tuition could not be verified."}
    except (ValueError, OSError, TimeoutError, UnicodeError) as exc:
        return {"status": "failed", "error": f"TRU official fee source unavailable: {str(exc)[:200]}"}
    payload = {**fee.normalized, "extraction_method": {"international_fee": fee.method}}
    evidence = {
        "field_key": fee.field_key, "value": fee.value, "normalized": fee.normalized,
        "source_url": (fee.extras or {}).get("tuition_source_url") or final,
        "method": fee.method, "snippet": fee.snippet, "extras": fee.extras,
        "confidence": fee.confidence, "selected": True, "decision_status": "selected",
    }
    return {"status": "success", "url": final, "payload": payload, "evidence": [evidence]}
