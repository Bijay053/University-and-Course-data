"""Backup endpoints (mirrors Node's /api/backup shape)."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Annotated
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db

router = APIRouter()

BACKUP_TABLES = [
    {"name": "courses_backup",               "source": "courses"},
    {"name": "fees_backup",                  "source": "fees"},
    {"name": "intakes_backup",               "source": "intakes"},
    {"name": "english_requirements_backup",  "source": "english_requirements"},
    {"name": "academic_requirements_backup", "source": "academic_requirements"},
    {"name": "scholarships_backup",          "source": "scholarships"},
]


@router.get("/backup")
async def get_backup(db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        stats = []
        for t in BACKUP_TABLES:
            tname = t["name"]
            count = (await db.execute(text(f"SELECT COUNT(*) AS total FROM {tname}"))).scalar_one()
            last = (await db.execute(text(f"SELECT MAX(backed_up_at) AS last FROM {tname}"))).scalar_one()
            snap_rows = (await db.execute(text(f"""
                SELECT backed_up_at::date AS snap_date, COUNT(*) AS rows
                FROM {tname}
                GROUP BY backed_up_at::date
                ORDER BY snap_date DESC LIMIT 30
            """))).all()
            snapshots = [{"snap_date": r.snap_date.isoformat() if r.snap_date else None,
                          "rows": int(r.rows)} for r in snap_rows]
            stats.append({
                "table": tname,
                "source": t["source"],
                "totalBackedUpRows": int(count or 0),
                "lastBackedUp": last.isoformat() if last else None,
                "snapshots": snapshots,
            })

        today_n = (await db.execute(text(
            "SELECT COUNT(*) FROM courses_backup WHERE backed_up_at::date = CURRENT_DATE"
        ))).scalar_one()
        today_done = int(today_n or 0) > 0
        last_overall = (await db.execute(text(
            "SELECT MAX(backed_up_at) FROM courses_backup"
        ))).scalar_one()

        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        return JSONResponse(content={
            "ok": True,
            "backups": stats,
            "scheduler": {
                "enabled": True,
                "checkIntervalMinutes": 60,
                "todayBackupDone": today_done,
                "lastBackupAt": last_overall.isoformat() if last_overall else None,
                "nextRunAt": tomorrow.isoformat() if today_done else "pending (within the hour)",
            },
        })
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@router.post("/backup")
async def post_backup(db: Annotated[AsyncSession, Depends(get_db)]):
    """Manual snapshot — copy each source table into its _backup table."""
    try:
        inserted = {}
        for t in BACKUP_TABLES:
            tname = t["name"]
            sname = t["source"]
            # Get source columns to match insert shape
            cols = (await db.execute(text(f"""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = '{sname}' ORDER BY ordinal_position
            """))).scalars().all()
            if not cols:
                inserted[tname] = 0
                continue
            col_list = ", ".join(cols)
            # Insert: backed_up_at = now, then source columns
            res = await db.execute(text(f"""
                INSERT INTO {tname} (backed_up_at, {col_list})
                SELECT NOW(), {col_list} FROM {sname}
            """))
            inserted[tname] = res.rowcount or 0
        await db.commit()
        return JSONResponse(content={
            "ok": True,
            "trigger": "manual",
            "inserted": inserted,
            "backedUpAt": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        await db.rollback()
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
