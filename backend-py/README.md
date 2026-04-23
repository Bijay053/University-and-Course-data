# Python Backend (FastAPI)

Parallel-deployment Python backend for University Portal. Replaces the Node/Express API in `artifacts/api-server/` after cutover.

## Status (2026-04-23)

| Phase | Description | Status |
|---|---|---|
| 0 | Pre-flight (deps, backup, inventory) | ✅ |
| 1 | FastAPI skeleton + health | ✅ |
| 2 | SQLAlchemy models matching existing schema | ✅ |
| 3 | Read-only endpoints (universities, courses, search, reviews, scrape jobs) | ✅ |
| 4 | Auth (cookie session) | ✅ |
| 5 | Scraper engine | 🟡 **Scaffold only** — browser pool, fetcher, stage_course, approve_course, extractor stubs. Real extraction logic must be ported from `artifacts/api-server/src/routes/scrape.ts` (~13K lines) one extractor at a time. |
| 6 | Celery bulk scraping | ✅ Skeleton ready |
| 7 | Gemini AI client + budget | ✅ |
| 8 | Deploy artifacts (systemd + nginx) | ✅ Files in `deploy/`, run on prod when ready |
| 9 | Cleanup of Node code | ❌ Per user instruction: do NOT delete Node files until cutover approved |

## Local dev

```bash
cd backend-py
pip install -e .
cp .env.example .env  # edit values
make dev              # uvicorn on $PORT (defaults 8000)
```

## Replit dev

The workflow `backend-py: FastAPI` runs uvicorn against the Replit DATABASE_URL (auto-converted from `postgres://` to `postgresql+asyncpg://`). Visit `http://localhost:$PORT/api/health`.

## Bug fixes already incorporated

All 5 demo-blocking Node bugs are baked into the Python implementation from day one:
- `approve_course` uses `func.lower(Course.name) == func.lower(...)` (Bug #1)
- `stage_course` returns `StageResult(saved=bool, reason=str)` dataclass (Bug #2)
- `UniversityCreate` schema rejects `Unknown` country/city, min length 2 (Bug #4)
- `auto_publish.should_auto_publish` does NOT require `international_fee`; English test is any-of IELTS/PTE/TOEFL/Cambridge/Duolingo (Bug #6)
- Rejection dedup window = 7 days (Bug #7)

## Cutover (do this on production when verified)

```bash
cd /root/University-and-Course-data
git pull
cd backend-py
python3.12 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
python scripts/verify_db_match.py     # ✅ all tables match
make run &                             # smoke test
curl http://localhost:8000/api/health
# Then install systemd units and switch nginx:
cp deploy/*.service /etc/systemd/system/
cp deploy/nginx.conf /etc/nginx/sites-available/default
systemctl daemon-reload
systemctl enable --now uni-api-py uni-celery
nginx -t && systemctl reload nginx
pm2 stop uni-api    # only after Python verified working through Nginx
```
