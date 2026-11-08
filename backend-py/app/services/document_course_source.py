"""Bounded course reads; redirects never skip source ownership or robots checks."""
import asyncio
from urllib.parse import urlsplit


async def read_course(url, home, reader, validator):
    visited, checks = set(), []
    current = validator(url, home)
    for _ in range(3):
        if current in visited:
            return {"verified": False, "error": "Official course redirect loop.", "sourceChecks": checks}
        visited.add(current)
        result = await reader(current, home)
        checks.append({"url": current, "verified": bool(result.get("verified")),
                       "failureStage": result.get("failureStage"), "httpStatus": result.get("httpStatus")})
        if result.get("verified"):
            return {**result, "sourceChecks": checks}
        redirect = result.get("redirectUrl")
        if redirect:
            try:
                target = validator(redirect, home)
                if urlsplit(target).scheme != "https":
                    raise ValueError("HTTPS required")
            except ValueError:
                return {**result, "error": "Course redirect is not an authorised official HTTPS URL.",
                        "sourceChecks": checks}
            current = target
            continue
        # Only retry an unclassified transport/research-limit failure once.
        # Never retry a robots failure, restriction, HTTP refusal or challenge.
        if (len(visited) == 1 and result.get("failureStage") == "source_fetch"
                and not result.get("accessIssue") and result.get("httpStatus") is None
                and not result.get("restrictionKind")):
            await asyncio.sleep(0.1)
            retry = await reader(current, home)
            checks.append({"url": current, "verified": bool(retry.get("verified")),
                           "failureStage": retry.get("failureStage"), "retry": True})
            return {**retry, "sourceChecks": checks}
        return {**result, "sourceChecks": checks}
    return {"verified": False, "error": "Official course redirect limit reached.", "sourceChecks": checks}
