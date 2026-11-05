import json,base64,gzip
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.models import University,ScrapedCourse
from app.models.audit import CourseAuditLog
from app.services.scrape_assistant import row_version
from tests.test_scrape_prompt_assistant import store,assistant_postgres


@pytest.mark.asyncio
async def test_reviewed_repair_roundtrip_and_stale_fence(store,monkeypatch,capsys):
    import app.database as database
    import app.managed_database_environment as managed
    monkeypatch.setattr(database,"AsyncSessionLocal",store)
    monkeypatch.setattr(database,"engine",SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(managed,"load_managed_database_environment",lambda:None)
    async with store() as db:
        db.add(University(id=110,name="Excelsia",country="Australia",city="Sydney"))
        await db.flush()
        row=await db.get(ScrapedCourse,1)
        row.university_id=110
        row.course_website="https://excelsia.edu.au/study/example/"
        await db.commit()
        plan=[{"id":1,"version":row_version(row),"courseUrl":row.course_website,
               "changes":{"intake_months":["February","July"]},
               "evidence":{"intake_months":{"url":row.course_website,"quote":"Intake: February and July"}}}]
    root=Path(__file__).resolve().parents[2]
    namespace={"PLAN":plan}
    exec((root/".local/excelsia_apply_verified.py").read_text().removesuffix("asyncio.run(main())\n"),namespace)
    await namespace["main"]()
    receipt=json.loads(gzip.decompress(base64.b64decode(capsys.readouterr().out.strip())))["applied"]
    with pytest.raises(AssertionError,match="Concurrent"):
        await namespace["main"]()
    undo={"PLAN":receipt}
    exec((root/".local/excelsia_undo_verified.py").read_text().removesuffix("asyncio.run(main())\n"),undo)
    await undo["main"]()
    async with store() as db:
        assert (await db.get(ScrapedCourse,1)).intake_months is None
        assert len((await db.execute(select(CourseAuditLog))).scalars().all())==2
