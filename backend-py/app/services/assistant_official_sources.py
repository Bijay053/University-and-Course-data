"""Bounded official guidance alternatives, never URL extension rewriting."""
import re
from urllib.parse import urlsplit

LSBU_ENGLISH_SOURCES = (
    "https://www.lsbu.ac.uk/__data/assets/pdf_file/0019/9280/english-language-qualifications-general.pdf",
    "https://www.lsbu.ac.uk/_media_old/pdf/guidelines/Eng-lang-requirements.pdf.pdf",
)

def is_english_guidance(source):
    return source.get("sourceType") == "pdf" and bool(re.search(
        r"eng(?:lish)?[-_ ]?lang|english-language", source.get("url", ""), re.I,
    ))


def english_guidance_alternatives(home, prompt, sources):
    """Candidates are unverified until the ordinary robots-fenced reader checks them."""
    if (urlsplit(home).hostname or "").lower().removeprefix("www.") != "lsbu.ac.uk":
        return []
    if not sources or not any(is_english_guidance(source) for source in sources):
        return []
    if any(source.get("verified") and is_english_guidance(source) for source in sources):
        return []
    seen = {source.get("url", "").split("#", 1)[0] for source in sources}
    return [url for url in LSBU_ENGLISH_SOURCES if url not in seen][:2]


def source_check_detail(result):
    """Keep permission failures separate from HTTP/path and document failures."""
    if result.get("verified"):
        return "Published URL preserved. Official document read; course applicability remains separate."
    stage = result.get("failureStage")
    detail = result.get("error", "Source could not be verified.")
    if stage == "robots":
        return f"URL preserved; robots.txt check failed before the document was requested. {detail}"
    if stage == "source_http":
        return f"URL preserved; document request returned HTTP {result.get('httpStatus')}. {detail}"
    if stage == "url_validation":
        return f"URL validation failed before a network request. {detail}"
    return detail


def gate_general_english_proposals(reply, sources):
    """A central PDF is research, not per-course authority for a whole-review fix."""
    if not any(source.get("verified") and is_english_guidance(source) for source in sources):
        return reply
    blocked = []
    for action in reply.get("actions", []):
        if (action.get("kind") == "fix_fields" and action.get("state") == "proposed"
                and "ielts_overall" in action.get("fields", [])):
            action.update(
                state="blocked", requiresConfirmation=True,
                error="General PDF guidance is not course-specific English evidence. "
                "Check each course, including health and postgraduate research exceptions, before proposing updates.",
            )
            blocked.append(action)
    if blocked:
        reply["content"] += (
            "\n\nNo whole-review IELTS correction was queued. The central guidance must first be "
            "matched to each course's requirements, especially health courses and postgraduate research."
        )
    return reply
