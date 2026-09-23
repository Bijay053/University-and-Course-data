"""Public provider evidence contains no response text, credentials or URLs."""
from __future__ import annotations

import re


def access_denied(status: int = 401) -> dict:
    return {
        "provider": "searchstax", "http_status": status,
        "kind": "provider_access_denied",
        "message": "The university's course search is not allowing access. Repair can check its official course pages instead.",
    }


class ProviderAccessDenied(RuntimeError):
    def __init__(self, status: int):
        self.provider_failure = access_denied(status)
        super().__init__(self.provider_failure["message"])


def recognize_failure(config: dict | None, *, status: str = "", total_found=0,
                      error_message: str = "", logs=()) -> dict | None:
    value = (config or {}).get("provider_failure")
    if isinstance(value, dict) and value.get("provider") == "searchstax" and value.get("kind") == "provider_access_denied":
        if value.get("http_status") in (401, 403):
            return access_denied(value["http_status"])
    # Only the exact historical provider failure wrapper on a failed empty job,
    # not an arbitrary URL containing "401" or a successful/partially found run.
    if status not in {"failed", "failed_provider", "error"} or total_found:
        return None
    if not str(error_message).startswith("SearchStax provider returned 0 links"):
        return None
    for message in [error_message, *logs]:
        if not isinstance(message, str):
            continue
        if message != error_message and not message.startswith(("[SEARCHSTAX links_only] WARNING: page fetch failed", "[SEARCHSTAX] WARNING: page fetch failed")):
            continue
        match = re.search(r"(?:Client error ['\"]|HTTP(?: status)?[ :=]+)(401|403)\b", message)
        if match:
            return access_denied(int(match[1]))
    return None


async def load_failure(db, job_id: str, config, *, status, total_found, error_message) -> dict | None:
    failure = recognize_failure(config, status=status, total_found=total_found, error_message=error_message)
    if failure or total_found or status not in {"failed", "failed_provider", "error"}:
        return failure
    if not str(error_message).startswith("SearchStax provider returned 0 links"):
        return None
    from sqlalchemy import text
    rows = (await db.execute(text(
        "SELECT payload->>'message' FROM scrape_runtime_logs "
        "WHERE runtime_job_id = :j AND payload->>'message' LIKE '[SEARCHSTAX%' "
        "ORDER BY sequence DESC LIMIT 50"
    ), {"j": job_id})).scalars().all()
    return recognize_failure(config, status=status, total_found=total_found,
                             error_message=error_message, logs=rows)


def sanitize_provider_logs(logs: list[dict]) -> list[dict]:
    """Legacy provider request URLs must not be copied into public diagnostics."""
    for row in logs:
        message = str(row.get("message") or "")
        if "searchstax" in message.lower() and re.search(r"\b(?:401|403)\b", message):
            row["message"] = access_denied()["message"]
            row["payload"] = {"message": row["message"], "level": "error"}
    return logs