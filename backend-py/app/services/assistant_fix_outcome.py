"""A finished queue job is not proof of a repaired course."""


def fix_outcome(action, job):
    results = job.get("results") or []
    expected = set(action.get("_ids") or [])
    complete = (
        bool(expected) and job.get("total") == len(expected)
        and len(results) == len(expected)
        and {r.get("id") for r in results} == expected
    )
    repaired = sum(r.get("outcome") == "updated" and bool(r.get("made_progress"))
                   and r.get("ok") is not False
                   for r in results)
    clear = sum(r.get("outcome") == "already_resolved" and not r.get("made_progress")
                and r.get("ok") is not False for r in results)
    failed = sum(r.get("ok") is False or r.get("outcome") == "failed" for r in results)
    unresolved = len(results) - repaired - clear - failed
    summary = (
        f"{len(results)}/{len(expected)} selected course results recorded: "
        f"{repaired} repairs verified, {clear} already clear, "
        f"{unresolved} without verified progress, {failed} failed. "
        "No courses were approved or published."
    )
    if not complete:
        summary += " The recorded results do not cover the exact requested scope."
    if not repaired and not clear:
        summary += " Finishing the job did not verify a correction."
    successful = complete and not failed and not unresolved and bool(repaired or clear)
    scope = {r["id"]: r for r in action.get("courseScope", [])}
    public = [{
        "id": r.get("id"),
        "url": scope.get(r.get("id"), {}).get("url") or r.get("url") or "",
        "status": r.get("outcome") or "unverified",
        "message": str(r.get("reason") or r.get("error") or r.get("outcome")
                       or "No verified field result was recorded.")[:700],
    } for r in results]
    return ("completed" if successful else "needs_review"), summary, public
