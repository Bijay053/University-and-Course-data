"""Bounded, verbatim HTML evidence windows, including later fee-table sections."""
import hashlib
import re

SCHEMA = 1
MAX_CHARS = 19_200


def html_windows(text, legacy):
    """Preserve existing quote identities; use spare budget for later facts.

    These are candidate quotations, never proof that an audience, course, year,
    or amount applies. Existing ownership/matching/approval checks still decide.
    """
    result = [dict(row) for row in legacy[:24]]
    remaining = MAX_CHARS - sum(len(row["text"]) for row in result)
    if remaining < 1600 or len(result) >= 24:
        return result
    # Full-programme totals must not lose to navigation or the first domestic
    # table. Include their surrounding labels, not a fabricated summary.
    primary = [
        m.start() for m in re.finditer(
            r"\b(?:estimated\s+)?tuition\s+total\s+for\s+(?:the\s+)?"
            r"[^.!?]{0,70}?\bprogram(?:me)?\b"
            r"|\b(?:full|total)[ -]+(?:course|program(?:me)?)[ -]+(?:tuition|fees?|cost)\b",
            text, re.I,
        )
    ]
    secondary = []
    for cue in ("international", "IELTS", "requirements", "tuition", "fees"):
        first = middle = last = None
        distance = len(text) + 1
        for match in re.finditer(re.escape(cue), text, re.I):
            position = match.start()
            if first is None:
                first = position
            if abs(position - len(text) // 2) < distance:
                middle, distance = position, abs(position - len(text) // 2)
            last = position
        if first is not None:
            secondary.extend(dict.fromkeys([last, middle, first]))
    # Interleave late document coverage with exact totals. Do not claim that a
    # bounded selection represents every row or every selectable programme.
    starts = []
    primary_set = set(primary)
    for position in primary + secondary:
        start = max(0, position - (1250 if position in primary_set else 950))
        if any(abs(start - old) < 300 for old in starts):
            continue
        starts.append(start)
        end = min(position + 200, start + 1600) if position in primary_set else start + 1600
        quote = text[start:min(end, start + remaining)]
        if not quote or len(quote) < 12 or any(quote in r["text"] for r in result):
            continue
        result.append({"id": "h" + hashlib.sha256(quote.encode()).hexdigest()[:20],
                       "text": quote})
        remaining -= len(quote)
        if len(result) >= 24 or remaining < 1600:
            break
    return result


