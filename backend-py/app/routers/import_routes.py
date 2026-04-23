"""Stub /api/import endpoints for UI compatibility."""
from typing import Annotated
from fastapi import APIRouter, Depends, UploadFile, File
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db

router = APIRouter(prefix="/import", tags=["import"])


@router.get("/history")
async def import_history(db: Annotated[AsyncSession, Depends(get_db)]) -> list:
    """Return list of past import jobs. Empty list if table doesn't exist."""
    try:
        rows = await db.execute(text(
            "SELECT id, file_name, status, total_rows, imported_rows, "
            "skipped_rows, error_rows, created_at, completed_at "
            "FROM import_jobs ORDER BY created_at DESC LIMIT 50"
        ))
        return [
            {
                "id": r[0],
                "fileName": r[1],
                "status": r[2],
                "totalRows": r[3],
                "importedRows": r[4],
                "skippedRows": r[5],
                "errorRows": r[6],
                "createdAt": r[7].isoformat() if r[7] else None,
                "completedAt": r[8].isoformat() if r[8] else None,
            }
            for r in rows.fetchall()
        ]
    except Exception:
        return []


@router.post("/excel")
async def import_excel(file: UploadFile = File(...)) -> dict:
    """Stub: accepts Excel upload. Real impl pending."""
    return {
        "ok": False,
        "message": "Excel import not yet implemented in Python backend. Use /api/universities/bulk-import for CSV upload instead.",
        "fileName": file.filename,
    }
