"""University-scoped name resolution; never let a sampled row choose the target."""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select

from app.models import Course, ScrapedCourse
from app.services.scrape_assistant_tools import official_url

AWARD = re.compile(r"\b(?:bachelor|master|graduate|postgraduate|undergraduate|doctor|diploma|certificate|associate|phd|bsc|msc|mba|llm)\b", re.I)
PREFIX = re.compile(
    r"^(?:(?:graduate|postgraduate|undergraduate)\s+)?"
    r"(?:bachelor|master|doctor|diploma|certificate|associate(?: degree)?|phd|bsc|msc|mba|llm)"
    r"(?:\s+(?:of|in))?\s+", re.I,
)
CONTEXTUAL = re.compile(r"\b(?:this|that|same|again|it|retry|results?|progress)\b", re.I)
MONTH = r"January|February|March|April|May|June|July|August|September|October|November|December"
MONTH_ALIASES = {alias.casefold(): month for month in MONTH.split("|")
                 for alias in (month, month[:3])} | {"sept": "September"}
PROMPT_MONTH = "|".join(sorted(MONTH_ALIASES, key=len, reverse=True))
DATED_TITLE = re.compile(rf"^(.*?)\s*\(\s*({MONTH})\s+(20\d{{2}})\s*\)\s*$", re.I)
DATED_URL = re.compile(rf"^(.*)/({MONTH})-(20\d{{2}})/?$", re.I)


def family_identity(item):
    """Group only matching name/URL intake suffixes, never delivery variants."""
    from urllib.parse import urlsplit, urlunsplit
    title = item["title"].strip()
    parts = urlsplit(item["url"])
    dated = DATED_TITLE.fullmatch(title)
    path = DATED_URL.fullmatch(parts.path)
    if dated:
        if (not path or parts.query
                or dated[2].casefold() != path[2].casefold()
                or dated[3] != path[3]):
            return (normal(title), item["url"].rstrip("/"))
        title, course_path = dated[1], path[1]
    elif path:
        # An unlabelled or mismatched dated URL is not proof of intake identity.
        return (normal(title), item["url"].rstrip("/"))
    else:
        course_path = parts.path.rstrip("/")
    return (normal(title), urlunsplit((parts.scheme, parts.netloc, course_path, parts.query, "")))


def family_matches(prompt, choices):
    """A short award title targets its options; option dates still narrow it."""
    text = " " + normal(positive_request(prompt)) + " "
    found = [item for item in choices
             if " " + family_identity(item)[0] + " " in text]
    # Retain longest-title preference: MSc X is not MSc X with placement.
    found = [item for item in found if not any(
        family_identity(item)[0] != family_identity(other)[0]
        and (" " + family_identity(item)[0] + " ") in
            (" " + family_identity(other)[0] + " ")
        for other in found
    )]
    dates = re.findall(rf"\b({PROMPT_MONTH})\.?\s+(20\d{{2}})\b", positive_request(prompt), re.I)
    if dates:
        selected = {normal(MONTH_ALIASES[month.casefold()] + " " + year) for month, year in dates}
        found = [item for item in found if (date := DATED_TITLE.fullmatch(item["title"]))
                 and normal(date[2] + " " + date[3]) in selected]
    return found


def name_predicate(column, value):
    """Literal, case-insensitive tool lookup, including labelled options."""
    value = value.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return column.ilike(value, escape="\\") | column.ilike(value + " (%)", escape="\\")


def normal(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def positive_request(prompt):
    value = re.split(r"\b(?:i meant|i mean|actually|rather check)\b", prompt, flags=re.I)[-1]
    value = re.sub(r"^\s*no\s*[,!]\s*", "", value, flags=re.I)
    if re.match(r"^\s*not\b", value, re.I):
        parts = re.split(r",|\bbut\b", value, maxsplit=1, flags=re.I)
        return parts[1] if len(parts) > 1 else ""
    return re.split(r"\b(?:not|rather than|instead of|don't mean|do not mean)\b", value, maxsplit=1, flags=re.I)[0]


def candidates(rows, home):
    unique = {}
    for title, url in rows:
        if not title or not url or title.startswith(("http:", "https:")):
            continue
        try:
            url = official_url(url, home)
        except (ValueError, TypeError):
            continue
        # Same award/URL repeated in historical reviews is one choice, but
        # different awards sharing a URL must remain separate choices.
        unique[(normal(title), url.rstrip("/"))] = {"title": title, "url": url}
    return sorted(unique.values(), key=lambda item: (normal(item["title"]), item["url"]))


def matches(prompt, choices):
    text = " " + normal(positive_request(prompt)) + " "
    families = family_matches(prompt, choices)
    if families:
        return families
    exact = [item for item in choices if " " + normal(item["title"]) + " " in text]
    if exact:
        # A full longer title is not also a request for its shorter prefix.
        return [item for item in exact if not any(
            normal(item["title"]) != normal(other["title"])
            and (" " + normal(item["title"]) + " ") in (" " + normal(other["title"]) + " ")
            for other in exact
        )]
    if AWARD.search(positive_request(prompt)):
        # A qualification is explicit; never substitute another award in the
        # same subject just because the requested title was not found.
        tail = positive_request(prompt)[AWARD.search(positive_request(prompt)).start():]
        tail = re.split(r"\b(?:fees?|ielts|duration|requirements?|please|again|is|was|does)\b",
                        tail, maxsplit=1, flags=re.I)[0]
        prefix = normal(tail)
        return [item for item in choices if len(prefix.split()) >= 3
                and normal(item["title"]).startswith(prefix + " ")]
    ranked = []
    for item in choices:
        words = PREFIX.sub("", normal(item["title"])).split()
        lengths = [size for size in range(1, len(words) + 1)
                   if len(part := " ".join(words[:size])) >= 5 and " " + part + " " in text]
        if lengths:
            ranked.append((max(lengths), item))
    best = max((size for size, _ in ranked), default=0)
    return [item for size, item in ranked if size == best]


def result(choices, *, incomplete=False):
    incomplete = incomplete or len(choices) > 10
    same_family = bool(choices) and len({family_identity(item) for item in choices}) == 1
    return {"state": "resolved" if same_family and not incomplete else "choice",
            "choices": choices[:10], "incomplete": incomplete}


async def resolve(prompt, messages, ctx, factory, catalogue_reader):
    """Return None for a non-targeted prompt; unresolved names fail closed."""
    prior = next((m for m in reversed(messages[:-1]) if m["role"] == "assistant"), {})
    previous_choices = candidates(
        [(item.get("title"), item.get("url")) for item in prior.get("courseChoices", [])],
        ctx["home"],
    )
    # Preserve the displayed order rather than the inventory's sort order.
    indexed = {item["url"] + "\0" + item["title"]: item for item in previous_choices}
    previous_choices = [indexed[item["url"] + "\0" + item["title"]]
                        for item in prior.get("courseChoices", [])
                        if item.get("url", "") + "\0" + item.get("title", "") in indexed]
    selection = re.fullmatch(r"\s*(?:option\s*|number\s*|#)?(\d{1,2})[.)]?\s*", prompt, re.I)
    if previous_choices and selection:
        index = int(selection[1]) - 1
        answer = result([previous_choices[index]]) if 0 <= index < len(previous_choices) else result(previous_choices)
        answer["requestPrompt"] = prior.get("courseRequest", prompt)
        return answer

    async with factory() as db:
        published = (await db.execute(select(Course.name, Course.course_website).where(
            Course.university_id == ctx["university_id"],
        ).order_by(Course.id).limit(2001))).all()
        pending = (await db.execute(select(ScrapedCourse.course_name, ScrapedCourse.course_website).where(
            ScrapedCourse.university_id == ctx["university_id"],
            ScrapedCourse.status.in_(["pending", "review", "review_ready"]),
        ).order_by(ScrapedCourse.id.desc()).limit(2001))).all()
    incomplete = len(published) > 2000 or len(pending) > 2000
    choices = candidates([*published[:2000], *pending[:2000]], ctx["home"])
    found = matches(prompt, choices)
    if found:
        answer = result(found, incomplete=incomplete)
        if len(found) == 1 and found[0] in previous_choices and normal(prompt) == normal(found[0]["title"]):
            answer["requestPrompt"] = prior.get("courseRequest", prompt)
        return answer
    # A title-only answer may select a previously verified catalogue candidate
    # which is not yet present in either database inventory.
    found = matches(prompt, previous_choices)
    if found:
        return {**result(found), "requestPrompt": prior.get("courseRequest", prompt)}
    target = re.sub(r"^(?:please\s+)?(?:check|what about|look at)\s+", "", prompt.strip(), flags=re.I)
    generic = {"why", "what", "how", "this", "that", "it", "same", "again", "do", "yes", "no",
               "all", "course", "courses", "fee", "fees", "ielts", "english", "duration",
               "location", "verify", "fix", "unverified", "requirements", "still", "issue",
               "hello", "hi", "thanks", "thank", "you", "band", "bands", "international",
               "go", "ahead", "run", "start", "continue", "okay", "ok", "proceed", "help",
               "data", "information", "check", "repair", "missing", "results", "progress",
               "these", "those", "they", "them", "scores", "test", "score", "amount",
               "tuition", "campus", "please"}
    title_only = 0 < len(target.split()) <= 10 and not (set(normal(target).split()) & generic)
    explicit_name = bool(AWARD.search(prompt) or title_only or re.search(
        r"\bcourse\s+(?:called|named)\b", prompt, re.I))
    if not explicit_name:
        if previous_choices:
            return {**result(previous_choices, incomplete=prior.get("courseChoiceIncomplete", False)),
                    "requestPrompt": prior.get("courseRequest", prompt)}
        if prior.get("courseNameUnresolved"):
            return {"state": "unresolved", "choices": [], "requestPrompt": prior.get("courseRequest", prompt)}
        previous = candidates(
            [(item.get("title"), item.get("url")) for item in prior.get("resolvedCourses", [])],
            ctx["home"],
        )
        if previous:
            return {**result(previous), "inherited": True}
    if not explicit_name:
        return None

    # Only the bounded, official-site reader can contribute catalogue titles;
    # model-generated URLs and unrelated review samples are not candidates.
    catalogue = await catalogue_reader(ctx["config"], ctx["home"], ctx["university_id"], factory)
    verified = any(source.get("status") == "checked" for source in catalogue.get("sources", []))
    catalogue_choices = candidates([
        (item.get("title"), item.get("url")) for item in catalogue.get("courses", [])
    ], ctx["home"]) if verified else []
    found = matches(prompt, catalogue_choices)
    if found:
        return result(found, incomplete=incomplete)
    return {"state": "unresolved", "choices": [], "incomplete": incomplete}


def clarification(resolution):
    choices = resolution["choices"]
    if choices:
        options = "\n".join(f"{index}. {item['title']} — {item['url']}"
                            for index, item in enumerate(choices, 1))
        return (
            "Which course do you mean? These awards match within this university:\n\n"
            + options + "\n\nReply with the number or full course title. "
            + ("The bounded inventory may contain further matches; please be specific. " if resolution.get("incomplete") else "")
            + "I have not selected a course or started a correction."
        )
    return (
        "I could not match that course name to this university's records or the official catalogue pages checked. "
        "This does not prove the course is missing. Please provide its full award title or official course URL. "
        "I have not used a different course from the open review, and no corrections were started."
    )
