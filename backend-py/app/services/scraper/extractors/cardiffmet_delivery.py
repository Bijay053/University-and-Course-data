"""Delivery authority from a course's own facts or explicit PGCE referral."""
import re

from .cardiffmet_facts import PGCE, _facts, _main, _verified_location, _write, is_course


def _owned_main(html):
    main = _main(html)
    if main is not None:
        for node in main.select(
            "[class*='related'],[class*='recommend'],[class*='course-card']"
        ):
            node.decompose()
    return main


def _online_exception(main, facts):
    delivery = " ".join(facts.get(label, "") for label in (
        "delivery", "delivery mode", "study mode", "mode of study",
    ))
    return bool(
        re.search(r"\bonline\b|distance learning", delivery, re.I)
        or re.search(
            r"\b(?:delivered|taught|studied)\s+(?:entirely\s+|exclusively\s+)?online\b"
            r"|\bonline.only delivery\b|\bdistance learning\b",
            main.get_text(" ", strip=True), re.I,
        )
    )


def apply_authority(html, url, payload, evidence, sources=None):
    """Never infer delivery from online applications, full-time load or menus."""
    if not is_course(url):
        return
    main = _owned_main(html)
    if main is None or main.find("h1") is None:
        return
    title = main.find("h1").get_text(" ", strip=True)
    if re.search(r"\bonline(?:\s+degree)?$|\bodl\b", title, re.I):
        return
    facts = _facts(main)
    if _online_exception(main, facts):
        return
    source = url
    location = facts.get("location", "")
    quote = f"Current course: {title}\nLocation: {location}"
    if not location:
        # Conflicting selectable locations are not an absent fact.
        if any(
            label.get_text(" ", strip=True).casefold() == "location"
            for item in main.select(".block-12__item")
            for label in item.find_all(["h3", "h4"])
        ):
            return
        referrals = [
            a for a in main.find_all("a", href=True)
            if a["href"].rstrip("/") in {
                "/courses/postgraduate/pgce-secondary", PGCE.rstrip("/"),
            } and re.search(
                r"PGCE Secondary course page", a.get_text(" ", strip=True), re.I,
            )
        ]
        if not re.match(r"PGCE Secondary\b", title, re.I) or not referrals:
            return
        parent = _owned_main((sources or {}).get(PGCE, ""))
        if parent is None or parent.find("h1") is None:
            return
        parent_title = parent.find("h1").get_text(" ", strip=True)
        if not re.match(
            r"(?:PGCE Secondary\b|Secondary \(11-18 Age Range\) with QTS - PGCE$)",
            parent_title, re.I,
        ):
            return
        parent_facts = _facts(parent)
        if _online_exception(parent, parent_facts):
            return
        location = parent_facts.get("location", "")
        source = PGCE
        quote = (
            f"Current course: {title}\nCourse source: {url}\n"
            f"Explicit referral: PGCE Secondary course page\n"
            f"Programme: {parent_title}\nLocation: {location}"
        )
    if not _verified_location(location):
        return
    # A labelled physical teaching campus is stronger than a broad AI/CSS
    # match on "apply online". Genuine online exceptions above remain unknown
    # here and continue through the normal eligibility gate.
    fields = [("course_location", location)]
    # A physical campus rules out exclusive online delivery, not the online
    # component of an already-established blended offering.
    if str(payload.get("study_mode") or "").casefold() != "blended":
        fields.append(("study_mode", "On Campus"))
    for field, value in fields:
        _write(
            payload, evidence, field, value, quote, source,
            "delivery.cardiffmet_course_location", replace=True,
        )
    payload["online_only"] = False
    payload["online_only_authoritative"] = False
