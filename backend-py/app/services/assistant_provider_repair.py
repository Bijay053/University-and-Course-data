"""Engine-independent chat entry point for supported university scraper repairs."""
import re
from difflib import SequenceMatcher
from app.models import ScrapeRuntimeJob
from app.models.scrape_assistant import ScrapeAssistantThread


_SCRAPER = r"\b(?:scrap(?:e|er|ing|ping)|scap(?:e|er|ing|ting)|discovery|extraction pipeline)\b"
_PROVIDER_TARGET = r"\b(?:scraper|scaper|scraping|scaping|scapting|scrapping|discovery|extraction pipeline)\b"
_FAILED_JOB_STATUSES = {"failed", "failed_degraded", "failed_provider", "interrupted", "error"}
_REPEAT_COMPLAINT = re.compile(r"\s*(?:still (?:the )?(?:same (?:bug|issue|error)|not working)|same bug)[.!? ]*", re.I)


def normalize_scraper_words(prompt):
    """Correct scraper-command typos, never course names, URLs or stored facts."""
    def word(match):
        value = match.group()
        if value.casefold() in {"scrape", "scraper", "scraping", "scrapping"}:
            return value
        if 6 <= len(value) <= 12 and re.match(r"(?:scra|scap)", value, re.I) and any(
            SequenceMatcher(None, value.casefold(), target).ratio() >= 0.8
            for target in ("scraping", "scraper")
        ):
            return "scraping"
        return value
    return re.sub(r"https?://\S+|[a-zA-Z]+", lambda m:
                  m.group() if m.group().lower().startswith(("https://", "http://"))
                  else word(m), prompt)


def filter_diagnostic(prompt):
    """Recognise pasted diagnostics as context, not as proof or executable code."""
    text = re.sub(r"https?://\S+", "", prompt)
    return bool(re.search(
        r"\bURL filter excluded\s+\d+(?:\.\d+)?%\s+of discovered links\b"
        r"|\ballow_url_patterns\s+matched\s+\d+\s+links?\s+and excluded\s+\d+\b"
        r"|\[FILTER\]\s+\d+\s+raw\s*[→>-]+\s*\d+\s+extractable",
        text, re.I,
    ))


def failed_job_followup(prompt):
    """Recognise shorthand only; the caller must separately verify failed state."""
    words = re.findall(r"[a-z]+", prompt.casefold())
    # Tolerate close misspellings of "scraping" only in this restricted
    # failed-job vocabulary. Never fuzzy-match course names or field edits.
    words = [
        "scraping" if 6 <= len(word) <= 12 and re.match(r"(?:scra|scap)", word) and any(
            SequenceMatcher(None, word, target).ratio() >= 0.8
            for target in ("scraping", "scraper")
        ) else word
        for word in words
    ]
    allowed = {
        "why", "how", "what", "did", "does", "is", "has", "it", "this", "that",
        "the", "job", "run", "fail", "failed", "failing", "failure", "fix",
        "repair", "resolve", "bug", "bugs", "error", "errors", "issue", "problem",
        "stop", "stopped", "again", "still", "not", "working", "work", "and",
        "please", "can", "you", "do", "just", "only", "explain", "diagnose",
        "don", "t", "dont", "never", "now", "went", "wrong", "happened",
        "happens", "happening", "to", "with", "here",
        "scraping", "scraper", "university", "provider",
    }
    return bool(words and set(words) <= allowed
                and set(words) & {"why", "how", "what", "fix", "repair", "resolve", "diagnose", "explain"})


def requested(prompt, messages=(), *, failed_job=False):
    prompt = normalize_scraper_words(prompt)
    # A prohibition is not a request to repair the discovery pipeline. Keep
    # source/field investigations on their own routes even when they say not
    # to launch discovery, or describe a possible source-access failure.
    prompt = re.sub(
        r"\b(?:do not|don't|dont|never|without)\s+"
        r"(?:re[- ]?run(?:ning)?|run(?:ning)?|start(?:ing)?|trigger(?:ing)?|launch(?:ing)?|perform(?:ing)?)\s+"
        r"(?:(?:a|an|the|any|new|fresh|full|catalogue|catalog|course|university)\s+){0,5}"
        r"(?:discovery|scrap(?:e|ing)|extraction(?:\s+pipeline)?)\b",
        "", prompt, flags=re.I,
    )
    # A field edit or document request must never become a discovery repair.
    if re.search(r"\b(?:pdf|document|upload)\b", prompt, re.I):
        return False
    if (re.search(r"\b(?:intakes?|ielts|location|course name)\b", prompt, re.I)
            and not re.search(_PROVIDER_TARGET, prompt, re.I)):
        return False
    if filter_diagnostic(prompt):
        return True
    if re.search(_SCRAPER, prompt, re.I):
        return bool(re.search(r"\b(?:fix|repair|bug|wrong|why|diagnose|fail\w*|issue|missing|problem)\b", prompt, re.I))
    if failed_job and failed_job_followup(prompt):
        return True
    if re.fullmatch(r"\s*(?:fix (?:this|it)|repair (?:this|it)|do it|still broken)[.!? ]*", prompt, re.I):
        previous = next((m for m in reversed(messages) if m.get("role") == "assistant"), {})
        return bool(previous.get("providerRepair"))
    return False


def wants_repair(prompt, messages=()):
    from app.services.scrape_assistant_autorun import execution_requested
    if not execution_requested(prompt):
        return False
    if re.search(r"\b(?:fix|repair|resolve|recover|do it)\b", prompt, re.I):
        return True
    if filter_diagnostic(prompt) and not re.search(r"\b(?:why|explain|diagnose|only|just)\b", prompt, re.I):
        # A log-only follow-up continues the immediately preceding user repair
        # request, never an instruction found in an assistant answer or website.
        prior = next((m.get("content", "") for m in reversed(messages[:-1])
                      if m.get("role") == "user"), "")
        return requested(prior) and wants_repair(prior)
    return False


def answer(report):
    counts = report["counts"]
    text = (
        f"Scrape status: {report['status']}. "
        f"{counts['processed']} processed from {counts['candidates']} final extraction candidates; "
        f"{counts['staged']} staged, {counts['skipped']} skipped, {counts['errors']} recorded errors. "
        "Staging counts do not establish complete catalogue coverage or field accuracy."
    )
    pipeline = report.get("pipelineEvidence") or {}
    if pipeline:
        text += (
            f"\n\nStored discovery/filter evidence: {pipeline['rawCandidates']} raw candidates "
            f"became {pipeline['extractableCandidates']} extractable URLs; "
            f"{pipeline['filterDropped']} were excluded by the URL filter. "
            "Raw candidates may include navigation, legacy or non-course pages; "
            "these counts do not prove that every excluded URL is a valid course."
        )
        if pipeline["rawCandidates"] > 0 and pipeline["extractableCandidates"] == 0:
            text += (
                " This is a discovery/filter collapse, even though the job recorded zero "
                "per-course extraction errors. Zero pending rows do not prevent provider-level diagnosis."
            )
    if (report["status"] in _FAILED_JOB_STATUSES and counts["staged"] == 0
            and not (pipeline.get("rawCandidates", 0) > 0
                     and pipeline.get("extractableCandidates") == 0)):
        text += "\n\nZero pending rows do not prevent provider-level diagnosis."
    for observation in report.get("logEvidence", {}).get("observations", []):
        text += "\n\n" + observation["detail"]
        for url in observation.get("urls", []):
            text += "\n" + url
        if observation.get("counts"):
            text += "\n" + ", ".join(f"{key}: {value}" for key, value in observation["counts"].items())
    return text


async def handle(service, thread_id, turn_id, run_id, ctx, prompt):
    # Return quickly on unrelated prompts, without database or network work.
    repeat = bool(_REPEAT_COMPLAINT.fullmatch(prompt))
    if not repeat and not requested(prompt) and not failed_job_followup(prompt) and not re.fullmatch(
        r"\s*(?:fix (?:this|it)|repair (?:this|it)|do it|still broken)[.!? ]*", prompt, re.I
    ):
        return False
    async with service.AsyncSessionLocal() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        if not thread or thread.job_id != ctx["job"]["id"]:
            return False
        job = await db.get(ScrapeRuntimeJob, thread.job_id)
        if not job:
            raise ValueError("Scrape job no longer exists.")
        if not repeat and not requested(prompt, thread.messages or [],
                         failed_job=getattr(job, "status", None) in _FAILED_JOB_STATUSES):
            return False
        from app.services.assistant_staff_values import parse
        staff = parse(prompt, thread.messages or [])
        if staff and staff.values:
            return False
        university_id = job.university_id
    from app.services.scrape_agent_v2.tools import DataTools
    tools = DataTools(service, thread_id, turn_id, run_id,
                      {"job": {"id": ctx["job"]["id"]}, "university_id": university_id}, prompt)
    if repeat:
        # Only continue an actual provider-repair discussion, never hijack
        # a terse follow-up to a field edit or an unrelated provider's chat.
        prior = next((m for m in reversed(thread.messages or [])
                      if m.get("role") == "assistant"), {})
        if not prior.get("providerRepair"):
            return False
    # DataTools rechecks turn ownership, live actor permissions and provider scope.
    report = await tools.execute("diagnose_scraper", {}, "provider-diagnosis")
    previous = await tools.execute("read_scraper_repair", {}, "provider-last-verification")
    previous = previous if isinstance(previous, dict) else {}
    content, actions = answer(report), []
    if previous.get("ok") and (
        previous.get("status") in {"queued", "running"}
        or previous.get("phase") in {"verifying", "verification_queued"}
    ):
        # A new chat turn is not permission to duplicate an existing repair.
        # Keep the original run's counters separate from its fresh verification.
        run = previous.get("verificationRun") or {}
        content = "A repair is already in progress for this review."
        if run:
            content += (
                f"\n\nLatest verification: {run.get('processed') or 0} processed "
                f"from {run.get('total') or 0} candidates; "
                f"{run.get('staged') or 0} staged, "
                f"{run.get('skipped') or 0} skipped, "
                f"{run.get('errors') or 0} recorded errors."
            )
            if run.get("releaseRevision"):
                content += f" Worker release: {run['releaseRevision']}."
        content += (
            "\n\nThe original scrape's failed-run counters are historical; they are "
            "not the progress of this verification. No duplicate repair or verification "
            "scrape was started. A running repair does not establish successful recovery. "
            "Nothing was approved or published."
        )
        await service.save_research_reply(thread_id, turn_id, run_id, {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": content, "actions": [], "sources": [],
            "providerRepair": report, "currentProviderRepair": previous,
        }, detail="Read existing repair progress; no duplicate work started.",
            phase="Existing scraper repair is in progress")
        return True
    if repeat:
        comparison = previous.get("verification") or {}
        if previous.get("phase") == "needs_review" and comparison:
            staged = comparison.get("cumulative_staged_courses", 0)
            fields = ", ".join(str(f).removesuffix("_pct").replace("_", " ")
                               for f in comparison.get("unresolved_fields") or [])
            content += f"\n\nThe last verification staged {staged} courses but did not verify a complete repair."
            if fields:
                content += f" Fields still incomplete: {fields}."
            content += " No repeat scrape has been started."
        else:
            content += ("\n\nLast repair status for this review: "
                        f"{previous.get('phase') or previous.get('status') or 'unavailable'}. "
                        "No repeat scrape has been started.")
    if wants_repair(prompt, thread.messages or []) and report["repairAvailable"]:
        result = await tools.execute("repair_scraper", {}, "provider-repair")
        if result.get("proposal"):
            actions = [result["proposal"]]
            content += (
                "\n\nI prepared a request to test a possible scraper repair. No fix has been validated for this request yet. "
                "The test can select up to 50 courses, so batch approval is required. "
                "No repair has run yet and no course has been published."
            )
        else:
            content += "\n\n" + result.get("reason", "Repair could not be prepared; no success is claimed.")
    elif not report["repairAvailable"]:
        content += "\n\n" + (report.get("blockedReason") or "No supported automatic repair was identified.")
    elif not repeat:
        content += "\n\nA supported repair is available. This was a diagnostic-only request; no repair was started."
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": content, "actions": actions, "sources": [],
        "providerRepair": report,
    }, detail="Stored scrape evidence checked; no website requests or course writes.",
        phase="Scraper diagnosis complete — see repair outcome")
    return True
