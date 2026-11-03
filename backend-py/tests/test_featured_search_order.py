"""Featured institutions lead default search before pagination, not explicit sorts."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.database import engine
from app.dependencies import get_db
from app.main import app


FEATURED_ORDER = (
    "c.featured DESC, "
    "CASE WHEN c.featured THEN c.featured_priority ELSE 0 END DESC, "
)


def _capture_order(monkeypatch, query):
    result = MagicMock()
    result.mappings.return_value.all.return_value = []
    result.scalar_one.return_value = 0
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    async def override():
        yield session

    monkeypatch.setitem(app.dependency_overrides, get_db, override)
    response = TestClient(app).get("/api/search/courses" + query)
    assert response.status_code == 200, response.text
    sql = str(session.execute.await_args_list[0].args[0])
    return sql.rsplit("ORDER BY ", 1)[1].split("LIMIT :limit")[0].strip()


@pytest.mark.parametrize("query", [
    "", "?sort=relevance", "?q=science", "?course_name=science",
    "?country=Australia&intakes=February&page=2&limit=2",
])
def test_default_search_promotes_featured_before_rank_and_pagination(monkeypatch, query):
    order = _capture_order(monkeypatch, query)
    assert order.startswith(FEATURED_ORDER)
    assert order.endswith("c.course_name, c.id")
    assert ("rank DESC NULLS LAST" in order) == (
        "q=" in query or "course_name=" in query
    )


@pytest.mark.parametrize("sort", [
    "name", "alpha", "duration", "duration_asc", "duration_desc",
    "fee_asc", "fee_desc",
])
def test_explicit_sort_is_not_overridden_by_featured(monkeypatch, sort):
    order = _capture_order(monkeypatch, f"?sort={sort}&currency=AUD")
    assert "c.featured" not in order
    if sort in {"name", "alpha"}:
        assert order.startswith("c.course_name ASC")
    elif sort.startswith("duration"):
        assert "c.duration_years" in order
    else:
        assert "fee_amount" in order


@pytest.mark.asyncio
@pytest.mark.parametrize("query, expected", [
    ("", [3, 5, 2, 1, 4]),
    ("?q=science", [3, 5, 2, 4, 1]),
])
async def test_real_database_featured_priority_and_stable_pages(monkeypatch, query, expected):
    order = _capture_order(monkeypatch, query)
    # Execute the endpoint's actual ordering against read-only VALUES. A private
    # pool avoids borrowing connections owned by a different pytest event loop.
    owned_engine = create_async_engine(engine.url, poolclass=NullPool)
    try:
        async with owned_engine.connect() as connection:
            seen = []
            for offset in (0, 2, 4):
                result = await connection.execute(text(f"""
                    WITH course_search_view
                        (id, course_name, featured, featured_priority, rank) AS (
                        VALUES
                            (1, 'A science', FALSE, 999, 10),
                            (2, 'Z science', TRUE, 10, 1),
                            (3, 'Y science', TRUE, 20, 0),
                            (4, 'Z science', FALSE, 0, 20),
                            (5, 'Y science', TRUE, 20, 0)
                    )
                    SELECT c.id FROM course_search_view c
                    ORDER BY {order}
                    LIMIT :limit OFFSET :offset
                """), {"limit": 2, "offset": offset})
                seen.extend(result.scalars().all())
            assert seen == expected
            assert len(seen) == len(set(seen))
    finally:
        await owned_engine.dispose()
