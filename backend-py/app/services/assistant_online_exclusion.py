"""University-scoped online-only checks; never reject from stored mode labels."""
import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

SCHEMA = 1
_COMMAND = re.compile(r"\b(?:reject|exclude|remove)\b.{0,70}\bonline(?:[- ]only)?\b"
                      r"|\bonline[- ]only\b.{0,50}\b(?:reject|exclude|remove)\b", re.I)
_FOLLOWUP = re.compile(r"^\s*(?:please\s+)?(?:do it|go ahead|yes|proceed|run it)[.!]?\s*$", re.I)


def intent(prompt, messages=()):
    from app.services.scrape_assistant_autorun import execution_requested
    from app.services.assistant_delivery_review import pending_delivery_question
    instruction = re.sub(r"https?://\S+", "", prompt).replace("’", "'")
    if re.search(
        r"\b(?:do not|don't|dont|never)\s+(?:reject|exclude|remove|change|fix)\b"
        r"|\bread[- ]only\b|\b(?:just|only)\s+explain\b"
        r"|\b(?:all|every)\s+(?:universities|providers)\b"
        r"|\b(?:flags?|warnings?|labels?)\b", instruction, re.I
    ):
        return None
    if (re.match(r"\s*(?:why|how|what|did|have|has|were|was)\b", instruction, re.I)
            and _COMMAND.search(instruction)):
        return None
    if _COMMAND.search(instruction) and re.search(r"\b(?:courses?|records?|rows?)\b", instruction, re.I):
        return {"prompt": prompt, "execute": execution_requested(prompt)}
    if pending_delivery_question(prompt) and re.search(r"\b(?:fix|check|verify|review)\b", prompt, re.I):
        return {"prompt": prompt, "execute": False}
    if _FOLLOWUP.fullmatch(prompt):
        previous = [m.get("content", "") for m in messages if m.get("role") == "user"]
        if previous and previous[-1] == prompt:
            previous.pop()
        for prior in reversed(previous):
            if _FOLLOWUP.fullmatch(prior):
                continue
            # Only the last substantive USER request supplies authority.
            # An assistant's sampled answer never becomes a rejection rule.
            return intent(prior) if _COMMAND.search(prior) else None
    return None


def selection(prompt, samples):
    # Research-wide "others" must not expand an explicit write selector.
    # Unsupported exception clauses need clarification, not a guessed deletion.
    if re.search(r"\b(?:except|excluding|but not)\b", prompt, re.I):
        return []
    if re.search(r"\bleave\b", prompt, re.I) and not re.search(
        r"\bleave\s+(?:all\s+)?(?:others?|the rest)\s+unchanged\b", prompt, re.I
    ):
        return []
    positive = re.split(r"\bleave\b", prompt, maxsplit=1, flags=re.I)[0]
    selected_ids = {
        int(number)
        for group in re.findall(
            r"\b(?:rows?|records?|ids?)\s*#?\s*(\d+(?:\s*(?:,|and|&)\s*#?\d+)*)",
            positive, re.I)
        for number in re.findall(r"\d+", group)
    }
    if selected_ids:
        return [row for row in samples if row["id"] in selected_ids]
    key = lambda value: " ".join(re.findall(r"\w+", value.casefold()))
    rows = [row for row in samples if f" {key(row['name'])} " in f" {key(positive)} "]
    if rows:
        return rows
    urls = re.findall(r"https?://[^\s)]+", positive)
    if urls:
        return [row for row in samples if row["url"].rstrip("/") in
                {url.rstrip("/.,") for url in urls}]
    # An unmatched singular course name must never expand into a whole review.
    return samples if re.search(r"\b(?:courses|records|rows|all|every)\b", positive, re.I) else []


def evidence(html, url):
    """Derive a bounded proof from course-owned DOM, not joined cache excerpts."""
    from app.services.scraper.extractors.course_header_delivery import course_header_delivery, _mode
    if re.search(r"/(?:news|blogs?|events?)/", urlsplit(url).path, re.I):
        return {"state": "unverified", "reason": "News, blog and event pages are not course-delivery evidence."}
    if any(key not in {"international", "students", "year", "utm_source", "utm_medium", "utm_campaign"}
           for key in parse_qs(urlsplit(url).query)):
        return {"state": "unverified", "reason": "Selected course option was not verified."}
    soup = BeautifulSoup(html, "html.parser")
    for node in list(soup.select(
        "script,style,noscript,nav,footer,[role='navigation'],[class*='related'],[class*='recommend']"
    )):
        node.decompose()
    headings = soup.find_all("h1")
    if len(headings) != 1:
        return {"state": "unverified", "reason": "A unique course heading was not verified."}
    name = headings[0].get_text(" ", strip=True)
    root = soup.find("main") or soup.find("article")
    if root is None:
        return {"state": "unverified", "name": name, "reason": "Course-owned main content was not verified."}
    primary = course_header_delivery(html)
    labelled = []
    if primary and primary[0] is not None:
        labelled.append(primary)
    else:
        label_root = soup.select_one(".course-page-header-wrapper") or root
        for node in label_root.find_all(["p", "dt"]):
            if any("hidden" in parent.attrs or parent.get("aria-hidden") == "true"
                   or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", parent.get("style", ""), re.I)
                   for parent in [node, *node.parents] if getattr(parent, "attrs", None) is not None):
                continue
            text = node.get_text(" ", strip=True)
            if node.name == "dt" and node.find_next_sibling("dd"):
                # The evidence quote keeps the DOM's punctuation exactly.
                text += " " + node.find_next_sibling("dd").get_text(" ", strip=True)
            match = re.fullmatch(r"(Delivery(?: mode)?|Mode of study|Study mode|Location)(?:\s*:\s*|\s+)(.{1,90})",
                                 text, re.I)
            if match:
                value = re.sub(r"\bonline learning\b", "online", match[2], flags=re.I)
                labelled.append((_mode(value, location=match[1].lower() == "location"), text))
    modes = {mode for mode, _ in labelled}
    if not modes or None in modes or len(modes) != 1:
        return {"state": "unverified", "name": name, "reason": "Delivery labels were absent or conflicting."}
    mode = next(iter(modes))
    if any(re.search(r"\b(?:ask|contact|enquire|admissions|unknown|tbc)\b", quote, re.I)
           for _, quote in labelled):
        return {"state": "unverified", "name": name, "reason": "Delivery label requires clarification."}
    if mode != "Online":
        return {"state": "mixed_mode" if mode == "Blended" else "on_campus",
                "name": name, "reason": "Course-owned delivery is not online-only.",
                "deliveryQuote": labelled[0][1]}
    # A primary Online label must not hide an international/campus panel or
    # another course option. Ambiguous audience/option applicability is a hold.
    for node in root.find_all(["p", "dt"]):
        text = node.get_text(" ", strip=True)
        if node.name == "dt" and node.find_next_sibling("dd"):
            text += " " + node.find_next_sibling("dd").get_text(" ", strip=True)
        match = re.fullmatch(
            r"(?:International\s+)?(Location|Campus|Delivery(?: mode)?|Study mode)"
            r"(?:\s*:\s*|\s+)(.{1,90})", text, re.I)
        if match and _mode(match[2], location=match[1].lower() in {"location", "campus"}) in {"On Campus", "Blended"}:
            return {"state": "unverified", "name": name,
                    "reason": "Another course-owned delivery/location label needs audience or option verification.",
                    "deliveryQuote": labelled[0][1], "campusQuote": text}
    # Retain mandatory attendance even inside a collapsed course accordion.
    # Optional visits, admissions interviews and navigation are not teaching.
    for node in root.find_all(["p", "li", "dd"]):
        for sentence in re.split(r"(?<=[.!?])\s+", node.get_text(" ", strip=True)):
            if len(sentence) > 1800 or re.search(r"\b(?:not required|not compulsory|no requirement|no mandatory)\b",
                                                sentence, re.I):
                continue
            campus = re.search(r"\b(?:on[- ]campus|in[- ]person|face[- ]to[- ]face)\b", sentence, re.I)
            required = re.search(r"\b(?:required|must|mandatory|compulsory)\b", sentence, re.I)
            teaching = re.search(r"\b(?:teaching|taught|classes|sessions|residency|workshops|lectures)\b",
                                 sentence, re.I)
            if campus and required and teaching:
                return {"state": "mixed_mode", "name": name,
                        "reason": "Required in-person teaching prevents online-only exclusion.",
                        "deliveryQuote": labelled[0][1], "campusQuote": sentence}
            if (campus and teaching
                    and not re.search(r"\b(?:optional|may|can choose|not required)\b", sentence, re.I)):
                return {"state": "unverified", "name": name,
                        "reason": "Campus teaching is mentioned but its applicability is unclear.",
                        "deliveryQuote": labelled[0][1], "campusQuote": sentence}
    return {"state": "checked", "name": name, "quote": labelled[0][1],
            "reason": "Course-owned delivery is Online; no compulsory campus teaching was established."}


async def check(service, thread_id, turn_id, run_id, row, home):
    from app.services.assistant_course_names import family_identity
    from app.services.assistant_source_gate import same_course_url
    from app.services.assistant_source_cache import cached_source
    from app.services.scrape_assistant_tools import official_url, inspect_source, quote_in_source
    url = official_url(row["url"], home)
    source = await cached_source(service, thread_id, turn_id, run_id, url, home,
                                 inspect_source, 12, online_exclusion_schema=SCHEMA)
    if not source.get("verified") or source.get("sourceType") != "html":
        return None, "unavailable"
    if (source.get("_onlineExclusionSchema") != SCHEMA
            or not same_course_url(url, source.get("url"))):
        return None, "unverified"
    proof = source.get("_onlineExclusion") or {}
    # Remove a dated title suffix only when the selected URL proves the same
    # month/year; never erase a delivery variant or a mismatched intake.
    key = lambda value: family_identity({"title": value or "", "url": url})[0]
    if not key(row["name"]) or key(row["name"]) not in {
        key(proof.get("name")), key(source.get("courseTitle")),
    }:
        return None, "unverified"
    state, quote = proof.get("state", "unverified"), proof.get("quote")
    if state == "checked" and quote and quote_in_source(quote, source):
        return quote, "checked"
    return None, state if state in {"mixed_mode", "on_campus"} else "unverified"


async def confirm_followup(service, thread_id, turn_id, run_id, ctx, prompt):
    """A clear chat approval continues the latest exact preview, without re-fetching."""
    if not _FOLLOWUP.fullmatch(prompt):
        return False
    from copy import deepcopy
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.services.assistant_eligibility import persist
    async with service.AsyncSessionLocal() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        if (not thread or thread.turn_id != turn_id or thread.run_id != run_id
                or thread.status != "researching"):
            return True
        previous = next((m for m in reversed(thread.messages)
                         if m.get("role") == "assistant"), {})
        plans = [a for a in previous.get("actions", [])
                 if a.get("kind") == "exclude_online" and a.get("state") == "proposed"]
        if len(plans) != 1:
            return False
        plan = deepcopy(plans[0])
    saved = plan.get("_ctx", {})
    if saved.get("university_id") != ctx["university_id"] or saved.get("_eligibilityKind") != "online":
        return False
    saved["_confirmedActionId"] = plan["id"]
    await persist(service, thread_id, turn_id, run_id, saved, plan["_rows"],
                  {int(k): tuple(v) for k, v in plan["_results"].items()}, confirmed=True)
    return True
