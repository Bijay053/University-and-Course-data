"""Stub /api/import endpoints for UI compatibility."""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
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
    """Stub: accepts Excel upload. Real implementation pending.

    Returns HTTP 501 with an ``error`` field so the frontend's existing error
    handler displays a clear message instead of rendering an empty
    "Import Complete" card (the previous behaviour silently dropped the file
    and showed blank counts).
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "error": (
                "Excel import is not yet implemented in the Python backend. "
                "For now, please convert the file to CSV and use the "
                "Universities → Bulk Import workflow instead. "
                f"(Received file: {file.filename})"
            ),
        },
    )
