"""Resolve explicitly university-qualified existing-course requests."""
import re
from urllib.parse import urlsplit


def existing_course_scope(prompt, samples, *, university_name="", home=""):
    from app.services.assistant_eligibility import scope

    compact = lambda value: "".join(re.findall(r"\w+", value.casefold()))
    aliases = {compact(university_name)}
    aliases.add(compact(re.sub(r"\b(?:the|of|university|college)\b", "", university_name, flags=re.I)))
    host = (urlsplit(home).hostname or "").split(".")
    if len(host) >= 2:
        suffix = ".".join(host[-2:])
        label = host[-3] if suffix in {"ac.uk", "co.uk", "edu.au", "ac.nz", "ac.za"} and len(host) >= 3 else host[-2]
        aliases.add(compact(label))
    aliases = {alias for alias in aliases if len(alias) >= 3}
    pattern = re.compile(
        r"\b(all|every|each)\s+"
        r"((?:(?:the|existing|remaining|selected|pending|current)\s+)*)"
        r"([\w &'’-]{2,100}?)\s+(courses?|records?|rows?|programmes?|programs?|degrees?)\b",
        re.I,
    )

    def replace(match):
        if compact(match[3]) not in aliases:
            return match[0]
        # A provider-wide request must not swallow an explicit subset.
        tail = prompt[match.end():]
        if re.match(r"\s+(?:named|called|with|whose|which|that|where|offered)\b", tail, re.I):
            return match[0]
        if re.match(r"\s+in\b(?!\s+(?:(?:this|the|current|open)\s+)*review\b)", tail, re.I):
            return match[0]
        return f"{match[1]} {match[2]}{match[4]}"

    return scope(pattern.sub(replace, prompt), samples)
