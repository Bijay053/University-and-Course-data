import json,base64,gzip
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.models import ScrapedCourse
from app.services.scrape_assistant import row_version
from tests.test_scrape_prompt_assistant import store,assistant_postgres

@pytest.mark.asyncio
@pytest.mark.parametrize("stale",[False,True])
async def test_repair_undo_and_version_fence(store,monkeypatch,capsys,stale):
    import app.database as database
    import app.managed_database_environment as managed
    monkeypatch.setattr(database,"AsyncSessionLocal",store)
    monkeypatch.setattr(database,"engine",SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(managed,"load_managed_database_environment",lambda:None)
    async with store() as db:
        r=await db.get(ScrapedCourse,1)
        r.course_website="https://www.uws.ac.uk/study/undergraduate/undergraduate-course-search/accounting/"
        r.duration=None
        await db.commit()
        plan=[{"before":{"id":1,"university_id":r.university_id,"version":row_version(r),"course_website":r.course_website},
               "changes":{"course_name":"BAcc (Hons) Accounting","duration":4.0},
               "warnings":["Campus-specific starts: Paisley September"],
               "evidence":[{"field_key":k,"value":v,"confidence":1,"source_url":r.course_website,
                            "method":"uws.course_banner","snippet":str(v)}
                           for k,v in {"course_name":"BAcc (Hons) Accounting","duration":4.0}.items()]}]
        if stale:r.course_name="New staff name";await db.commit()
    ns={"PLAN":plan}
    exec((Path(__file__).resolve().parents[1]/"scripts/uws_pending_repair.py").read_text().removesuffix("asyncio.run(main())\n"),ns)
    if stale:
        with pytest.raises(AssertionError,match="Concurrent"):await ns["main"]()
        return
    await ns["main"]()
    receipt=json.loads(gzip.decompress(base64.b64decode(capsys.readouterr().out.strip())))
    ns["PLAN"]={"mode":"undo",**receipt}
    await ns["main"]()
    async with store() as db:
        assert (await db.get(ScrapedCourse,1)).duration is None
