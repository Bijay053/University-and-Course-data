"""Resolve course-owned intake facts without trusting screenshot/model values."""
import re
from urllib.parse import urlsplit


def intake_proof(source, url, name):
    if not source.get("verified"):
        return None
    candidates = []
    intake = source.get("_excelsiaIntake") or {}
    fold = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    placeholder = bool(re.fullmatch(r"(?:https?://)?(?:www\.)?excelsia\.edu\.au/?", name, re.I))
    identity_matches = ((intake.get("name") or "").casefold() == name.casefold()
                        or placeholder and fold(intake.get("name") or "") == fold(slug))
    if identity_matches and intake.get("months") and intake.get("quote"):
        candidates.append((intake["months"], intake["quote"]))
    from app.services.assistant_source_gate import same_course_url
    from app.services.scrape_assistant_tools import quote_in_source
    if same_course_url(url, source.get("url")):
        for fact in source.get("facts", []):
            months = (fact.get("normalized") or {}).get("intake_months")
            if (fact.get("field") == "intake_months"
                    and fact.get("method") == "intake:selected_start_option"
                    and months and quote_in_source(fact.get("quote") or "", source)):
                candidates.append((months, fact["quote"]))
    if len({tuple(months) for months, _ in candidates}) != 1:
        return None
    return candidates[0]
