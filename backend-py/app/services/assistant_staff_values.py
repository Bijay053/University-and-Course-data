"""Parse literal staff instructions, never model/tool/page text, into atomic edits."""
import re
from dataclasses import dataclass, field as dataclass_field

MONTHS = ("January February March April May June July August September October November December").split()
EDIT = re.compile(r"\b(?:add|set|change|update|edit)\b", re.I)
FOLLOW = re.compile(
    r"\s*(?:please\s+)?(?:add|apply|set|repeat)\s+(?:this|that|it|the latest edit)"
    r"\s+(?:in|to|on|for)\s+all\s+courses[.!]?\s*", re.I)


def record_kind(prompt):
    """Bare chat row numbers are pending-review IDs, not published course IDs."""
    if re.search(r"\b(?:row|record)\s*#?\s*\d+\b", prompt, re.I):
        return "published" if re.search(r"\bpublished\s+(?:row|record)\s*#?\s*\d+\b", prompt, re.I) else "pending"
    return None


def staff_scope(prompt, samples, *, all_courses=False):
    """Write targeting is not research scope: 'others' never expands a write."""
    exclusion = re.search(
        r";|\b(?:except|excluding|exclude|but not|leave|keep|without changing|do not|don't)\b",
        prompt, re.I,
    )
    target_text = prompt[:exclusion.start()] if exclusion else prompt
    if all_courses:
        # A provider repeat is supported; complex inclusion/exclusion sets need
        # an explicit row list instead of guessing what 'all except...' means.
        return [] if exclusion else samples
    ids = {int(n) for n in re.findall(r"\b(?:row|record)\s*#?\s*(\d+)\b", target_text, re.I)}
    if ids:
        return [row for row in samples if row["id"] in ids]
    urls = {u.rstrip("/.,);") for u in re.findall(r"https://[^\s]+", target_text)}
    if urls:
        return [row for row in samples if (row.get("url") or "").rstrip("/") in urls]
    normal = lambda text: " ".join(re.findall(r"\w+", (text or "").casefold()))
    target = normal(target_text)
    return [row for row in samples if normal(row.get("name")) and normal(row["name"]) in target]


@dataclass
class StaffRequest:
    values: dict
    all_courses: bool
    instruction: str
    error: str = ""
    missing_only: set = dataclass_field(default_factory=set)


def parse(prompt, messages=()):
    if prompt.strip() == "?" and any(
        m.get("role") == "user" and EDIT.search(m.get("content", ""))
        for m in messages
    ):
        previous = next((m.get("content", "") for m in reversed(messages)
                         if m.get("role") == "assistant"), "")
        return StaffRequest({}, False, prompt,
            previous or "Your last request was a field edit, not a course search. No additional changes have been made.")
    if re.fullmatch(r"\s*no need to match it just add (?:this|what I) requested[.!]?\s*", prompt, re.I):
        for index in range(len(messages) - 1, -1, -1):
            text = messages[index].get("content", "")
            if messages[index].get("role") == "user" and text != prompt and EDIT.search(text):
                return parse(text, messages[:index])
        return StaffRequest({}, False, prompt, "There is no earlier explicit field edit to apply.")
    clauses = [c.strip() for c in re.split(r"\n+|\balso\b", prompt, flags=re.I) if c.strip()]
    if len(clauses) > 1 and all(EDIT.search(c) for c in clauses):
        parts = [parse(c, ()) for c in clauses]
        if any(not p or p.error or not p.all_courses for p in parts):
            return StaffRequest({}, False, prompt, "Please give each edit an explicit value and the same all-courses scope. Nothing changed.")
        values, missing = {}, set()
        for p in parts:
            if set(values) & set(p.values):
                return StaffRequest({}, True, prompt, "The request edits the same field twice; specify one value.")
            values.update(p.values)
            missing.update(p.missing_only)
        return StaffRequest(values, True, prompt, missing_only=missing)
    from app.services.scrape_assistant_autorun import execution_requested
    if (not execution_requested(prompt) or
        re.match(r"\s*(?:explain|how|why|what|show|compare)\b", prompt, re.I) or
        re.search(r"\b(?:don't|do not|never)\s+(?:set|edit)\b", prompt, re.I)):
        return None
    if FOLLOW.fullmatch(prompt):
        history = list(messages)
        if history and history[-1].get("role") == "user" and history[-1].get("content") == prompt:
            history.pop()
        for index in range(len(history) - 1, -1, -1):
            message = history[index]
            if message.get("role") != "user":
                continue
            text = message.get("content", "")
            # An intervening edit is a hard boundary, even if unsupported or invalid.
            if EDIT.search(text) or FOLLOW.fullmatch(text):
                prior = parse(text, history[:index])
                if prior:
                    return StaffRequest(prior.values, True, prior.instruction, prior.error, prior.missing_only)
                return StaffRequest({}, True, text, "The latest edit has no reusable explicit values.")
        return StaffRequest({}, True, prompt, "There is no prior explicit staff edit to repeat.")
    match = re.search(
        r"\b(?:add|set|change|update|edit)\s+(?:the\s+)?(?:course\s+)?"
        r"(intakes?|fees?|tuition|ielts|duration|name|pte|toefl|cricos(?: code)?)"
        r"\b\s*(?:=|to\b|as\b|:)?\s*(.*)", prompt, re.I)
    if not match:
        # Keep legacy location grammar without allowing its history scan.
        from app.services.assistant_staff_locations import location_request
        location = location_request(prompt, [])
        return StaffRequest({"course_location": location[0]}, location[1], prompt) if location else None
    field, text = match[1].lower(), match[2].strip()
    all_courses = bool(re.search(r"\ball\s+(?:\w+\s+)?courses?\b", prompt, re.I))
    value = re.split(r"\s+(?:(?:on|in|to)\s+(?:all\b|row\b|record\b|course\b)|for\s+(?!20\d{2}\b))",
                     text, maxsplit=1, flags=re.I)[0].strip().rstrip(".!")
    request = StaffRequest({}, all_courses, prompt)
    # Bare source-recovery requests still belong to the evidence-backed path.
    if not value or re.match(r"^(?:from|using|based on|according to|missing\b)", value, re.I):
        return None
    if field.startswith("intake"):
        parts = re.split(r"\s*(?:,|/|&|\band\b)\s*", value, flags=re.I)
        lookup = {m.lower(): m for m in MONTHS}
        lookup.update({m[:3].lower(): m for m in MONTHS})
        if all(p.lower() in lookup for p in parts):
            request.values = {"intake_months": list(dict.fromkeys(lookup[p.lower()] for p in parts))}
    elif field in {"fee", "fees", "tuition"}:
        fee = re.fullmatch(
            r"([A-Z]{3})\s+(\d+(?:,\d{3})*(?:\.\d{1,2})?)\s+"
            r"(annual|per year|full course|per semester|per trimester|per month)"
            r"\s+(?:for\s+)?(20\d{2})", value, re.I)
        if fee:
            terms = {"annual":"Annual", "per year":"Annual", "full course":"Full Course",
                     "per semester":"Per Semester", "per trimester":"Per Trimester", "per month":"Per Month"}
            currency = fee[1].upper()
            # The portal accepts ISO-shaped codes; never infer a currency from "$".
            if re.fullmatch(r"[A-Z]{3}", currency):
                request.values = dict(international_fee=float(fee[2].replace(",", "")),
                                      currency=currency, fee_term=terms[fee[3].lower()], fee_year=int(fee[4]))
    elif field == "duration":
        duration = re.fullmatch(r"(\d+(?:\.\d+)?)\s+(years?|months?|weeks?)", value, re.I)
        if duration and float(duration[1]) > 0:
            request.values = {"duration": float(duration[1]), "duration_term": duration[2].rstrip("sS").title()}
    elif field in {"ielts", "pte", "toefl"}:
        component = re.fullmatch(r"(listening|reading|writing|speaking)\s*(?:to\s+|=\s*)?(\d+(?:\.\d+)?)", value, re.I)
        if field == "ielts" and component:
            number = float(component[2])
            if 0 < number <= 9 and number * 2 == int(number * 2):
                request.values = {"ielts_" + component[1].lower(): number}
        score = re.fullmatch(r"(?:overall\s+)?(\d+(?:\.\d+)?)"
                            r"(?:\s+(?:with\s+)?(?:no band below|each band|all bands)\s+(\d+(?:\.\d+)?))?", value, re.I)
        limit = {"ielts": 9, "pte": 90, "toefl": 120}[field]
        if score and 0 < float(score[1]) <= limit and (field == "ielts" or not score[2]):
            values = {field+"_overall": float(score[1])}
            if score[2]:
                values.update({f"ielts_{part}": float(score[2]) for part in ("listening", "reading", "writing", "speaking")})
            if field != "ielts" or all(0 < v <= 9 and v * 2 == int(v * 2) for v in values.values()):
                request.values = values
    elif field == "name" and value.startswith('"') and value.endswith('"') and len(value) > 2:
        request.values = {"course_name": value[1:-1]}
    elif field.startswith("cricos") and re.fullmatch(r"\d{6}[A-Z]", value, re.I):
        request.values = {"cricos_code": value.upper()}
    if not request.values:
        request.error = ("Supply an explicit value with its units. Tuition needs amount, ISO currency, "
                         "fee period and year (for example AUD 24000 annual 2026); "
                         "duration needs a number and unit; intake needs month names; IELTS needs a valid score.")
    if re.search(r"\bmissing\b|\bempty\b|\bblank\b", prompt, re.I):
        request.missing_only = set(request.values)
    return request
