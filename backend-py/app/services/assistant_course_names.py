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
    return {"state": "resolved" if len(choices) == 1 and not incomplete else "choice",
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
