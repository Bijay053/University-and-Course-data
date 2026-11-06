from pathlib import Path
from sqlalchemy import text,select
import pytest
from tests.test_scrape_prompt_assistant import store,assistant_postgres
from app.models import University
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services.scrape_assistant_autorun import choose_action

@pytest.mark.asyncio
@pytest.mark.parametrize("paused",[True,False])
@pytest.mark.parametrize("kind",["scan_catalogue","recover_catalogue","review_courses","repair_catalogue"])
async def test_pause_blocks_imports_but_keeps_field_fixes(store,paused,kind):
    async with store() as db:
        # Execute the multi-statement DDL through the driver's supported API.
        conn=await db.connection();raw=await conn.get_raw_connection()
        await raw.driver_connection.execute((Path(__file__).resolve().parents[1]/"scripts/assistant_import_pause.sql").read_text())
        uni=await db.get(University,1);uni.scrape_config={"assistant_imports_paused":paused}
        await db.flush()
        db.add(ScrapeAssistantThread(id="pause-test",job_id="source",actor_id=1,
            turn_id="turn",status="ready",messages=[{"role":"assistant","actions":[
                {"id":"import","state":"proposed","kind":kind,"courseCount":0},
                {"id":"edit","state":"proposed","kind":"fix_fields","courseCount":1}]}]))
        await db.commit()
    async with store() as db:
        t=await db.get(ScrapeAssistantThread,"pause-test")
        assert choose_action(t.messages[-1]["actions"])["id"]==("edit" if paused else "import")
