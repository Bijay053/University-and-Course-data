"""Durable access policies and observations, shared across users and providers.

Approval records permission, not verification of a document or its applicability.
No policy may override robots, network validation or a protection challenge.
"""
from datetime import datetime, timezone
import asyncio
import hashlib
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from app.models.source_access import UniversitySourceAccess
from app.services.scrape_assistant_tools import official_url

REUSE_SECONDS = 120


class AccessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = "official_https"
    restrictions: str = Field(default="", max_length=2000)
    alternatives: list[str] = Field(default_factory=list, max_length=5)
    authorization_reference: str = Field(default="", max_length=1000)

    @field_validator("method")
    @classmethod
    def method_allowed(cls, value):
        if value not in {"official_https", "authorized_upload", "authorization_required"}:
            raise ValueError("Only implemented, permitted access methods may be selected.")
        return value


def identity(url, home):
    url = official_url(url, home)
    parsed = urlsplit(url)
    # www and bare host are the same owner, matching official_url's ownership rule.
    origin = "https://" + parsed.hostname.lower().removeprefix("www.")
    return origin, url


def validate_policy(policy, home, url):
    policy = AccessPolicy.model_validate(policy).model_dump()
    urls = list(dict.fromkeys(official_url(u, home) for u in policy["alternatives"]))
    if official_url(url, home) in urls:
        raise ValueError("The primary source cannot also be its alternative.")
    policy["alternatives"] = urls
    return policy


async def entry(db, url, home):
    origin, url = identity(url, home)
    return await db.get(UniversitySourceAccess, (origin, url))


async def ensure(db, url, home):
    origin, url = identity(url, home)
    await db.execute(insert(UniversitySourceAccess).values(
        origin=origin, url=url, policy={}, observation={}, history=[],
    ).on_conflict_do_nothing(index_elements=["origin", "url"]))
    return (await db.execute(select(UniversitySourceAccess).where(
        UniversitySourceAccess.origin == origin, UniversitySourceAccess.url == url,
    ).with_for_update())).scalar_one()


async def set_policy(db, url, home, policy, actor_id):
    validated = validate_policy(policy, home, url)
    record = await ensure(db, url, home)
    now = datetime.now(timezone.utc)
    record.history = [*record.history, {
        "at": now.isoformat(), "actorId": actor_id,
        "before": record.policy, "after": validated,
    }]
    record.policy = validated
    record.updated_at = now
    return record


async def observe(db, url, home, result):
    record = await ensure(db, url, home)
    now = datetime.now(timezone.utc)
    # Whitelisted diagnostics only: never persist source text, query responses,
    # staff uploads, cookies, authorization headers or signed object URLs.
    record.observation = {
        k: result[k] for k in (
            "verified", "failureStage", "accessIssue", "httpStatus", "restrictionKind",
            "sourceRequestAttempted", "sourceType",
        ) if k in result
    } | {"checkedAt": now.isoformat()}
    if result.get("verified"):
        record.last_success_at = now
    record.updated_at = now


def refusal(record, now=None):
    """Never use a previous success as fresh document evidence."""
    if record is None:
        return None
    policy = record.policy or {}
    observation = record.observation or {}
    method = policy.get("method", "official_https")
    held = method in {"authorization_required", "authorized_upload"}
    recent = False
    if observation.get("verified") is False and observation.get("accessIssue"):
        try:
            age = ((now or datetime.now(timezone.utc)) -
                   datetime.fromisoformat(observation["checkedAt"])).total_seconds()
            recent = 0 <= age < REUSE_SECONDS
        except (KeyError, ValueError, TypeError):
            pass
    if not held and not recent:
        return None
    return {
        **observation, "url": record.url, "verified": False,
        "failureStage": observation.get("failureStage", "access_policy"),
        "sourceRequestAttempted": False, "_accessRegistryHit": True,
        "error": (
            "Automated access is paused for this document. Use an authorised upload "
            "or ask the university to authorise its official endpoint."
            if held else
            "This document's recent access check failed. The shared registry temporarily "
            "avoids repeating the same blocked request; use an authorised upload or a permitted alternative."
        ),
    }


def public_record(record):
    return {
        "url": record.url, "origin": record.origin,
        "policy": record.policy, "observation": record.observation,
        "lastSuccessAt": record.last_success_at.isoformat() if record.last_success_at else None,
        "updatedAt": record.updated_at.isoformat(), "history": record.history,
    }


async def guarded_read(session_factory, url, home, reader, timeout):
    """Coordinate simultaneous sessions without process/loop-owned locks.

    Transaction locks are released even on cancellation. A slow reader holds
    only its document lock, never a university-wide or course-row lock.
    """
    origin, url = identity(url, home)
    lock_key = int.from_bytes(hashlib.sha256(f"{origin}\n{url}".encode()).digest()[:8],
                              "big", signed=True)
    async with session_factory() as db:
        locked = (await db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                   {"key": lock_key})).scalar()
        if not locked:
            return {
                "url": url, "verified": False, "failureStage": "access_in_progress",
                "sourceRequestAttempted": False, "retryable": True,
                "error": "Another session is checking this document. Retry when that check finishes; no course changes were made.",
            }
        blocked = refusal(await entry(db, url, home))
        if blocked:
            return blocked
        result = await asyncio.wait_for(reader(url, home), timeout)
        await observe(db, url, home, result)
        await db.commit()
        return result
