"""Phase 14 — Autonomous Publishing & Review Engine.

Pipeline:
  scraped_courses (pending/review)
      ↓ compute_pub_score()          — composite 0-100 score
      ↓ apply_pub_decision()         — auto_publish / needs_review / hold
      ↓ execute_auto_publish()       — calls approve_course for auto_publish rows
      ↓ log to publishing_ledger
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.field_conflict import FieldConflict
from app.models.publishing_ledger import PublishingLedger
from app.models.scraped_course import ScrapedCourse
from app.models.university import University

log = logging.getLogger("uniportal")

# ── Scoring constants ──────────────────────────────────────────────────────────
# Weight distribution: completeness + confidence dominate (90%); conflict
# penalty can remove up to 30 points.
_W_COMPLETENESS = 0.45
_W_CONFIDENCE = 0.45
_W_CONFLICT_BASE = 0.10

# Critical field keys — conflicts on these block auto-publish
CRITICAL_FIELDS = frozenset(
    {"international_fee", "ielts_overall", "degree_level", "pte_overall", "toefl_overall"}
)

# Auto-publish threshold
_AUTO_PUBLISH_MIN = 90.0
_NEEDS_REVIEW_MIN = 70.0


# ── Score computation ──────────────────────────────────────────────────────────

def compute_pub_score(
    sc: ScrapedCourse,
    open_conflicts: int,
    critical_conflicts: int,
) -> dict[str, Any]:
    """Compute a 0-100 publishing confidence score for a staged course.

    Components
    ----------
    completeness (45%)  — how many of the 13 review fields are populated
    confidence   (45%)  — avg_verification_confidence or eligibility_confidence
    conflict_ok  (10%)  — passes through fully when no conflicts; penalised otherwise
    -conflict_penalty   — subtracted from raw: 15 pts per critical, 3 per open

    Decision thresholds
    -------------------
    ≥ 90 + 0 critical conflicts → auto_publish
    ≥ 70                        → needs_review
    < 70                        → hold
    """
    completeness = float(sc.completeness or 0)
    confidence = float(
        sc.avg_verification_confidence
        or sc.eligibility_confidence
        or completeness  # fallback — completeness is proxy when confidence not computed
    )
    confidence = min(confidence, 100.0)

    conflict_penalty = min(critical_conflicts * 15 + open_conflicts * 3, 30)
    conflict_ok = max(0.0, 100.0 - conflict_penalty * 3)  # scale to 0-100

    raw = (
        completeness * _W_COMPLETENESS
        + confidence * _W_CONFIDENCE
        + conflict_ok * _W_CONFLICT_BASE
    )
    pub_score = round(max(0.0, min(100.0, raw)), 1)

    # Decision
    if pub_score >= _AUTO_PUBLISH_MIN and critical_conflicts == 0:
        decision = "auto_publish"
        reason = f"Score {pub_score} ≥ {_AUTO_PUBLISH_MIN} with no critical conflicts"
    elif pub_score >= _NEEDS_REVIEW_MIN:
        decision = "needs_review"
        if critical_conflicts:
            reason = f"Score {pub_score} but {critical_conflicts} critical conflict(s) on {', '.join(CRITICAL_FIELDS & set()) or 'key field(s)'}"
        else:
            reason = f"Score {pub_score} in review range ({_NEEDS_REVIEW_MIN}–{_AUTO_PUBLISH_MIN})"
    else:
        decision = "hold"
        reason = f"Score {pub_score} < {_NEEDS_REVIEW_MIN} — insufficient completeness/confidence"

    return {
        "score": pub_score,
        "breakdown": {
            "completeness": round(completeness, 1),
            "confidence": round(confidence, 1),
            "open_conflicts": open_conflicts,
            "critical_conflicts": critical_conflicts,
            "conflict_penalty": conflict_penalty,
        },
        "decision": decision,
        "reason": reason,
    }


async def _count_conflicts(
    sc_id: int, db: AsyncSession
) -> tuple[int, int]:
    """Return (open_conflicts, critical_conflicts) for a staged course."""
    result = await db.execute(
        select(
            func.count(FieldConflict.id).label("total"),
            func.count(FieldConflict.id).filter(
                FieldConflict.field_key.in_(list(CRITICAL_FIELDS))
            ).label("critical"),
        ).where(
            FieldConflict.scraped_course_id == sc_id,
            FieldConflict.status == "open",
        )
    )
    row = result.one()
    return int(row.total or 0), int(row.critical or 0)


# ── Single-course helpers ──────────────────────────────────────────────────────

async def score_course(sc: ScrapedCourse, db: AsyncSession) -> dict[str, Any]:
    """Compute, store, and return the pub_score dict for one staged course."""
    open_c, crit_c = await _count_conflicts(sc.id, db)
    result = compute_pub_score(sc, open_c, crit_c)
    sc.pub_score = result["score"]
    sc.pub_score_breakdown = result["breakdown"]
    sc.pub_decision = result["decision"]
    sc.pub_decision_reason = result["reason"]
    return result


def _ledger_entry(sc: ScrapedCourse, action: str, actor: str, reason: str) -> PublishingLedger:
    return PublishingLedger(
        scraped_course_id=sc.id,
        university_id=sc.university_id,
        course_name=sc.course_name,
        action=action,
        pub_score=sc.pub_score,
        pub_score_breakdown=sc.pub_score_breakdown,
        actor=actor,
        reason=reason,
    )


# ── Batch publishing pass ──────────────────────────────────────────────────────

async def run_publishing_pass(
    db: AsyncSession,
    university_id: int | None = None,
) -> dict[str, int]:
    """Score all pending staged courses and auto-publish those that qualify.

    Returns counts: scored, auto_published, needs_review, held.
    """
    from app.services.scraper.approve_course import approve_scraped_course  # lazy import

    query = select(ScrapedCourse).where(
        ScrapedCourse.status.in_(["pending", "review"]),
        ScrapedCourse.auto_publish_status.in_(["pending_review", "ready", "review"]),
    )
    if university_id is not None:
        query = query.where(ScrapedCourse.university_id == university_id)

    result = await db.execute(query)
    courses = list(result.scalars())

    counts = {"scored": 0, "auto_published": 0, "needs_review": 0, "held": 0, "errors": 0}

    for sc in courses:
        try:
            scored = await score_course(sc, db)
            counts["scored"] += 1

            if scored["decision"] == "auto_publish":
                try:
                    await approve_scraped_course(sc.id, db)
                    db.add(_ledger_entry(sc, "auto_published", "system", scored["reason"]))
                    counts["auto_published"] += 1
                except Exception as e:
                    log.warning("Auto-publish failed for scraped_course %s: %s", sc.id, e)
                    counts["errors"] += 1
            elif scored["decision"] == "needs_review":
                sc.auto_publish_status = "review"
                db.add(_ledger_entry(sc, "queued_review", "system", scored["reason"]))
                counts["needs_review"] += 1
            else:
                sc.auto_publish_status = "pending_review"
                db.add(_ledger_entry(sc, "held", "system", scored["reason"]))
                counts["held"] += 1

        except Exception as e:
            log.error("Publishing pass error for sc %s: %s", getattr(sc, "id", "?"), e)
            counts["errors"] += 1

    await db.commit()
    log.info("Publishing pass complete: %s", counts)
    return counts


# ── Manual review actions ──────────────────────────────────────────────────────

async def manually_approve(sc_id: int, reason: str, db: AsyncSession) -> dict:
    """Human approval — promote to live courses."""
    from app.services.scraper.approve_course import approve_scraped_course

    result = await db.execute(select(ScrapedCourse).where(ScrapedCourse.id == sc_id))
    sc = result.scalar_one_or_none()
    if not sc:
        raise ValueError(f"ScrapedCourse {sc_id} not found")

    await approve_scraped_course(sc_id, db)
    db.add(_ledger_entry(sc, "manually_published", "human", reason or "Manual approval"))
    await db.commit()
    return {"ok": True, "action": "manually_published", "scraped_course_id": sc_id}


async def manually_reject(sc_id: int, reason: str, db: AsyncSession) -> dict:
    """Human rejection."""
    result = await db.execute(select(ScrapedCourse).where(ScrapedCourse.id == sc_id))
    sc = result.scalar_one_or_none()
    if not sc:
        raise ValueError(f"ScrapedCourse {sc_id} not found")

    sc.status = "rejected"
    sc.rejection_reason = reason or "Manual rejection"
    sc.reviewed_at = datetime.now(timezone.utc)
    db.add(_ledger_entry(sc, "rejected", "human", sc.rejection_reason))
    await db.commit()
    return {"ok": True, "action": "rejected", "scraped_course_id": sc_id}


async def manually_hold(sc_id: int, reason: str, db: AsyncSession) -> dict:
    """Hold for later (keeps status pending but marks pub_decision=hold)."""
    result = await db.execute(select(ScrapedCourse).where(ScrapedCourse.id == sc_id))
    sc = result.scalar_one_or_none()
    if not sc:
        raise ValueError(f"ScrapedCourse {sc_id} not found")

    sc.pub_decision = "hold"
    sc.pub_decision_reason = reason or "Manual hold"
    sc.auto_publish_status = "pending_review"
    db.add(_ledger_entry(sc, "held", "human", sc.pub_decision_reason))
    await db.commit()
    return {"ok": True, "action": "held", "scraped_course_id": sc_id}


# ── Dashboard stats ────────────────────────────────────────────────────────────

async def get_publishing_stats(db: AsyncSession) -> dict:
    """Aggregate stats for the publishing dashboard."""
    # Pending course counts by pub_decision
    dec_result = await db.execute(
        select(
            ScrapedCourse.pub_decision,
            func.count(ScrapedCourse.id).label("cnt"),
        )
        .where(ScrapedCourse.status.in_(["pending", "review"]))
        .group_by(ScrapedCourse.pub_decision)
    )
    by_decision: dict[str | None, int] = {}
    for row in dec_result:
        by_decision[row.pub_decision] = int(row.cnt)

    # Not-yet-scored (no pub_decision set) — these need a pass
    unscored = by_decision.get(None, 0)

    # Ledger counts — all time
    ledger_result = await db.execute(
        select(PublishingLedger.action, func.count(PublishingLedger.id).label("cnt"))
        .group_by(PublishingLedger.action)
    )
    by_action: dict[str, int] = {row.action: int(row.cnt) for row in ledger_result}

    auto_pub = by_action.get("auto_published", 0)
    manual_pub = by_action.get("manually_published", 0)
    total_pub = auto_pub + manual_pub
    auto_rate = round(auto_pub / total_pub * 100, 1) if total_pub else None

    # Ledger counts — today
    today_result = await db.execute(
        select(PublishingLedger.action, func.count(PublishingLedger.id).label("cnt"))
        .where(func.date(PublishingLedger.created_at) == func.current_date())
        .group_by(PublishingLedger.action)
    )
    today: dict[str, int] = {row.action: int(row.cnt) for row in today_result}

    # University counts
    uni_result = await db.execute(
        select(func.count(func.distinct(ScrapedCourse.university_id)))
        .where(
            ScrapedCourse.status.in_(["pending", "review"]),
            ScrapedCourse.pub_decision == "auto_publish",
        )
    )
    unis_ready = int(uni_result.scalar() or 0)

    return {
        "ready_to_publish": by_decision.get("auto_publish", 0),
        "needs_review": by_decision.get("needs_review", 0),
        "held": by_decision.get("hold", 0),
        "unscored": unscored,
        "universities_with_ready": unis_ready,
        "total_auto_published": auto_pub,
        "total_manually_published": manual_pub,
        "total_rejected": by_action.get("rejected", 0),
        "total_held": by_action.get("held", 0),
        "auto_publish_rate": auto_rate,
        "published_today": today.get("auto_published", 0) + today.get("manually_published", 0),
    }


# ── University publishing summary ─────────────────────────────────────────────

async def get_university_summary(db: AsyncSession) -> list[dict]:
    """Aggregate per-university publishing health for the management dashboard."""
    # Base aggregation — one row per university
    base_result = await db.execute(
        select(
            ScrapedCourse.university_id,
            University.name.label("university_name"),
            University.country.label("university_country"),
            func.count(ScrapedCourse.id).label("total_courses"),
            func.count(ScrapedCourse.id).filter(
                ScrapedCourse.pub_decision == "auto_publish"
            ).label("auto_published"),
            func.count(ScrapedCourse.id).filter(
                ScrapedCourse.pub_decision == "needs_review"
            ).label("needs_review"),
            func.count(ScrapedCourse.id).filter(
                ScrapedCourse.pub_decision == "hold"
            ).label("held"),
            func.avg(ScrapedCourse.pub_score).label("avg_pub_score"),
            func.avg(
                func.coalesce(
                    ScrapedCourse.avg_verification_confidence,
                    ScrapedCourse.eligibility_confidence,
                )
            ).label("avg_confidence"),
            func.avg(ScrapedCourse.completeness).label("avg_completeness"),
        )
        .join(University, University.id == ScrapedCourse.university_id)
        .where(
            ScrapedCourse.status.in_(["pending", "review"]),
            ScrapedCourse.pub_decision.isnot(None),
        )
        .group_by(ScrapedCourse.university_id, University.name, University.country)
        .order_by(University.name)
    )
    base_rows = base_result.all()

    if not base_rows:
        return []

    # JSONB conflict aggregation — single query for all universities
    conflict_result = await db.execute(text("""
        SELECT
            sc.university_id,
            SUM(COALESCE((sc.pub_score_breakdown->>'open_conflicts')::int, 0))
                AS total_open,
            SUM(COALESCE((sc.pub_score_breakdown->>'critical_conflicts')::int, 0))
                AS total_critical,
            SUM(CASE
                WHEN COALESCE((sc.pub_score_breakdown->>'critical_conflicts')::int, 0) = 0
                THEN 1 ELSE 0
            END) AS conflict_free_count
        FROM scraped_courses sc
        WHERE sc.status IN ('pending', 'review')
          AND sc.pub_decision IS NOT NULL
          AND sc.pub_score_breakdown IS NOT NULL
        GROUP BY sc.university_id
    """))
    conflict_map: dict[int, dict] = {
        row.university_id: {
            "open": int(row.total_open or 0),
            "critical": int(row.total_critical or 0),
            "conflict_free": int(row.conflict_free_count or 0),
        }
        for row in conflict_result.all()
    }

    out: list[dict] = []
    for row in base_rows:
        total = int(row.total_courses)
        avg_score = round(float(row.avg_pub_score or 0), 1)
        # Fallback: use avg_score when confidence not computed
        avg_conf = round(float(row.avg_confidence or avg_score), 1)
        avg_comp = round(float(row.avg_completeness or 0), 1)

        c = conflict_map.get(row.university_id, {"open": 0, "critical": 0, "conflict_free": total})
        conflict_free_rate = round(c["conflict_free"] / total * 100, 1) if total else 100.0

        # Health = 40% pub_score + 30% confidence + 20% completeness + 10% conflict-free rate
        health = round(
            0.40 * avg_score
            + 0.30 * avg_conf
            + 0.20 * avg_comp
            + 0.10 * conflict_free_rate,
            1,
        )
        health = min(100.0, max(0.0, health))

        if health >= 90:
            health_status = "Excellent"
        elif health >= 80:
            health_status = "Good"
        elif health >= 70:
            health_status = "Needs Attention"
        else:
            health_status = "At Risk"

        out.append({
            "university_id": row.university_id,
            "university_name": row.university_name,
            "university_country": row.university_country or "",
            "total_courses": total,
            "auto_published": int(row.auto_published),
            "needs_review": int(row.needs_review),
            "held": int(row.held),
            "avg_pub_score": avg_score,
            "avg_confidence": avg_conf,
            "avg_completeness": avg_comp,
            "total_open_conflicts": c["open"],
            "total_critical_conflicts": c["critical"],
            "conflict_free_rate": conflict_free_rate,
            "publish_health": health,
            "health_status": health_status,
        })

    # Worst health first so At Risk universities surface to top
    out.sort(key=lambda x: x["publish_health"])
    return out


# ── Review queue ───────────────────────────────────────────────────────────────

async def get_review_queue(
    db: AsyncSession,
    university_id: int | None = None,
    decision_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Return staged courses in the review queue, richest context first."""
    query = (
        select(ScrapedCourse, University.name.label("uni_name"), University.country.label("uni_country"))
        .join(University, University.id == ScrapedCourse.university_id)
        .where(
            ScrapedCourse.status.in_(["pending", "review"]),
            ScrapedCourse.pub_decision.in_(["needs_review", "hold", "auto_publish"]),
        )
        .order_by(
            # hold → needs_review → auto_publish; within band, highest score first
            case(
                (ScrapedCourse.pub_decision == "hold", 0),
                (ScrapedCourse.pub_decision == "needs_review", 1),
                else_=2,
            ),
            ScrapedCourse.pub_score.desc().nullslast(),
        )
        .limit(limit)
        .offset(offset)
    )
    if university_id is not None:
        query = query.where(ScrapedCourse.university_id == university_id)
    if decision_filter:
        query = query.where(ScrapedCourse.pub_decision == decision_filter)

    rows = (await db.execute(query)).all()

    out = []
    for sc, uni_name, uni_country in rows:
        open_c, crit_c = await _count_conflicts(sc.id, db)
        out.append({
            "id": sc.id,
            "university_id": sc.university_id,
            "university_name": uni_name,
            "university_country": uni_country,
            "course_name": sc.course_name,
            "degree_level": sc.degree_level,
            "pub_score": sc.pub_score,
            "pub_decision": sc.pub_decision,
            "pub_decision_reason": sc.pub_decision_reason,
            "pub_score_breakdown": sc.pub_score_breakdown,
            "completeness": sc.completeness,
            "avg_verification_confidence": sc.avg_verification_confidence,
            "eligibility_confidence": sc.eligibility_confidence,
            "open_conflicts": open_c,
            "critical_conflicts": crit_c,
            "international_fee": sc.international_fee,
            "ielts_overall": sc.ielts_overall,
            "status": sc.status,
            "auto_publish_status": sc.auto_publish_status,
            "created_at": sc.created_at.isoformat() if sc.created_at else None,
        })
    return out


# ── Ledger ─────────────────────────────────────────────────────────────────────

async def get_ledger(
    db: AsyncSession,
    university_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    query = (
        select(PublishingLedger, University.name.label("uni_name"))
        .join(University, University.id == PublishingLedger.university_id)
        .order_by(PublishingLedger.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if university_id is not None:
        query = query.where(PublishingLedger.university_id == university_id)

    rows = (await db.execute(query)).all()
    return [
        {
            "id": pl.id,
            "scraped_course_id": pl.scraped_course_id,
            "university_id": pl.university_id,
            "university_name": uni_name,
            "course_name": pl.course_name,
            "action": pl.action,
            "pub_score": pl.pub_score,
            "pub_score_breakdown": pl.pub_score_breakdown,
            "actor": pl.actor,
            "reason": pl.reason,
            "created_at": pl.created_at.isoformat() if pl.created_at else None,
        }
        for pl, uni_name in rows
    ]
