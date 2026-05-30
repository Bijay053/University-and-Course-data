---
name: Repair module arg order
description: Correct positional argument order for ai_extractor_repair functions
---

## The rule
In `backend-py/app/services/scraper/ai_extractor_repair.py`, the async functions take `scrape_run_id` FIRST, `db` SECOND:

```python
async def compute_field_fill_rates(scrape_run_id: int, db: Any) -> dict[str, float]: ...
async def fetch_repair_samples(scrape_run_id: int, db: Any, n: int = 3) -> list[tuple[str, str]]: ...
```

The `repair_extractor` Celery task must call them as:
```python
fill_rates = await compute_field_fill_rates(scrape_run_id, db)
samples = await fetch_repair_samples(scrape_run_id, db, n=5)
```

**Why:** This is the opposite of the FastAPI/SQLAlchemy convention where `db` often comes first. The repair module was written with run_id first to make the primary key explicit.

**How to apply:** Any new caller of these functions must respect this order. If you see `compute_field_fill_rates(db, ...)` that is a bug.
