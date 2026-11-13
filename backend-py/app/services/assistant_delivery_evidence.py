"""Course-owned delivery proofs shared by planning and the approval writer."""
import re

from app.services.assistant_source_gate import same_course_url
from app.services.scrape_assistant_tools import quote_in_source

METHOD = "study_mode:course_header_fact"


def delivery_proof(url, name, sources):
    """Never borrow a provider-wide, sibling-course or model-generated mode."""
    def title(value):
        return re.sub(r"\W+", "", value or "").casefold()

    candidates = []
    for source in sources or []:
        if (not source.get("verified") or source.get("sourceType") != "html"
                or not same_course_url(url, source.get("url"))
                or not name or title(source.get("courseTitle")) != title(name)):
            continue
        for fact in source.get("facts", []):
            value, quote = fact.get("value"), fact.get("quote")
            if (fact.get("field") == "study_mode" and fact.get("method") == METHOD
                    and value in {"On Campus", "Blended", "Online"}
                    and fact.get("normalized") == {"study_mode": value}
                    and isinstance(quote, str) and quote_in_source(quote, source)):
                candidates.append((value, quote, source))
    if len({value for value, _, _ in candidates}) != 1:
        return None
    # Online needs the existing full eligibility/quarantine path; this narrow
    # writer must not silently clear fees, campuses or alter published records.
    return next((p for p in candidates if p[0] != "Online"), None)
