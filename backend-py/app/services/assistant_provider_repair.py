"""Engine-independent chat entry point for supported university scraper repairs."""
import re
from app.models import ScrapeRuntimeJob
from app.models.scrape_assistant import ScrapeAssistantThread


_SCRAPER = r"\b(?:scrap(?:e|er|ing|ping)|scap(?:e|er|ing|ting)|discovery|extraction pipeline)\b"
_PROVIDER_TARGET = r"\b(?:scraper|scaper|scraping|scaping|scapting|scrapping|discovery|extraction pipeline)\b"


def requested(prompt, messages=()):
    # A field edit or document request must never become a discovery repair.
    if re.search(r"\b(?:pdf|document|upload)\b", prompt, re.I):
        return False
    if (re.search(r"\b(?:intakes?|ielts|location|course name)\b", prompt, re.I)
            and not re.search(_PROVIDER_TARGET, prompt, re.I)):
        return False
    if re.search(_SCRAPER, prompt, re.I):
        return bool(re.search(r"\b(?:fix|repair|bug|wrong|why|diagnose|fail\w*|issue|missing|problem)\b", prompt, re.I))
    if re.fullmatch(r"\s*(?:fix (?:this|it)|repair (?:this|it)|do it|still broken)[.!? ]*", prompt, re.I):
        previous = next((m for m in reversed(messages) if m.get("role") == "assistant"), {})
        return bool(previous.get("providerRepair"))
    return False


def wants_repair(prompt):
    from app.services.scrape_assistant_autorun import execution_requested
    return execution_requested(prompt) and bool(re.search(r"\b(?:fix|repair|resolve|recover|do it)\b", prompt, re.I))


def answer(report):
    counts = report["counts"]
    text = (
        f"Scrape status: {report['status']}. "
        f"{counts['processed']} processed from {counts['candidates']} candidates; "
        f"{counts['staged']} staged, {counts['skipped']} skipped, {counts['errors']} recorded errors. "
        "Staging counts do not establish complete catalogue coverage or field accuracy."
    )
    for observation in report.get("logEvidence", {}).get("observations", []):
        text += "\n\n" + observation["detail"]
        for url in observation.get("urls", []):
            text += "\n" + url
        if observation.get("counts"):
            text += "\n" + ", ".join(f"{key}: {value}" for key, value in observation["counts"].items())
    return text


async def handle(service, thread_id, turn_id, run_id, ctx, prompt):
    # Return quickly on unrelated prompts, without database or network work.
    if not requested(prompt) and not re.fullmatch(
        r"\s*(?:fix (?:this|it)|repair (?:this|it)|do it|still broken)[.!? ]*", prompt, re.I
    ):
        return False
    async with service.AsyncSessionLocal() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        if not thread or thread.job_id != ctx["job"]["id"] or not requested(prompt, thread.messages or []):
            return False
        from app.services.assistant_staff_values import parse
        staff = parse(prompt, thread.messages or [])
        if staff and staff.values:
            return False
        job = await db.get(ScrapeRuntimeJob, thread.job_id)
        if not job:
            raise ValueError("Scrape job no longer exists.")
        university_id = job.university_id
    from app.services.scrape_agent_v2.tools import DataTools
    tools = DataTools(service, thread_id, turn_id, run_id,
                      {"job": {"id": ctx["job"]["id"]}, "university_id": university_id}, prompt)
    # DataTools rechecks turn ownership, live actor permissions and provider scope.
    report = await tools.execute("diagnose_scraper", {}, "provider-diagnosis")
    content, actions = answer(report), []
    if wants_repair(prompt) and report["repairAvailable"]:
        result = await tools.execute("repair_scraper", {}, "provider-repair")
        if result.get("proposal"):
            actions = [result["proposal"]]
            content += (
                "\n\nI prepared a validated provider-configuration repair and isolated fresh verification. "
                "The test can select up to 50 courses, so batch approval is required. "
                "No repair has run yet and no course has been published."
            )
        else:
            content += "\n\n" + result.get("reason", "Repair could not be prepared; no success is claimed.")
    elif not report["repairAvailable"]:
        content += "\n\n" + (report.get("blockedReason") or "No supported automatic repair was identified.")
    else:
        content += "\n\nA supported repair is available. This was a diagnostic-only request; no repair was started."
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": content, "actions": actions, "sources": [],
        "providerRepair": report,
    }, detail="Stored scrape evidence checked; no website requests or course writes.",
        phase="Scraper diagnosis complete — see repair outcome")
    return True
