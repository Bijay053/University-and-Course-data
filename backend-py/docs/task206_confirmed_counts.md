# Task #206 — Confirmed Course Counts: Portsmouth & Ulster

**Date:** 2026-06-20

---

## Dev environment validation (Replit)

### Portsmouth (uni_id=2174)

| Metric | Value |
|---|---|
| Job ID | job_e7cb2e24bf4d |
| Status | completed |
| total_found | 421 |
| imported (staged) | **418** |
| errors | 0 |
| skipped | 3 |
| Threshold (≥250) | ✅ PASS |

All 418 staged rows have `status=pending` — ready for operator review/approve.

**YAML config active:** `backend-py/scraper_config/unis/port_2174.yaml`  
Discovery: `sitemap.xml?page=2` (explicitly configured) + `always_sitemap_supplement: true`.  
Staging flags: `skip_degree_qualifier_check: true`, `require_international_fee: false`.

### Ulster (uni_id=2176) — Batch 1 of 2 (dev)

| Metric | Value |
|---|---|
| Job ID | job_22a384c2da4f |
| Status | completed |
| total_found | 461 |
| imported (staged) | **461** |
| errors | 0 |
| skipped | 0 |
| Threshold (≥350) | ✅ PASS |

Staging breakdown:
- `pending / data_quality_failure`: 399 — completeness <85% (Wayback-archived pages lack fee tables)
- `pending / review`: 62 — staged, pending operator review

**YAML config active:** `backend-py/scraper_config/unis/ulster_2176.yaml`  
Discovery: `sitemap-courses.xml` (987 raw URLs) → year_dedup → 461 staged (batch 1 of 2).

---

## Production runbook

> Run these after `git pull origin main` on the droplet to pick up the YAML + sitemap_offset changes.

### Verify uni IDs differ from dev

```sql
-- Run on prod before triggering any scrape
SELECT id, name FROM universities WHERE name ILIKE '%portsmouth%';
SELECT id, name FROM universities WHERE name ILIKE '%ulster%';
```

Note: prod uni IDs may differ from dev (2174/2176). Use the prod IDs in all commands below.

### Portsmouth — prod scrape

```bash
cd /root/University-and-Course-data

# Trigger Portsmouth scrape via API (replace PORT_UNI_ID with prod ID)
curl -s -c /tmp/cookie.txt -b /tmp/cookie.txt \
  -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@university-portal.local","password":"Bijay@12345"}'

PORT_JOB=$(curl -s -c /tmp/cookie.txt -b /tmp/cookie.txt \
  -X POST http://localhost:8000/api/scrape/start \
  -H "Content-Type: application/json" \
  -d '{"universityId": PORT_UNI_ID}' | python3 -c "import sys,json;print(json.load(sys.stdin)['runtimeJobId'])")
echo "Portsmouth job: $PORT_JOB"

# Poll until complete
watch -n 30 "sudo -u postgres psql -d university_portal -c \"SELECT status,total_found,imported,errors,heartbeat_at FROM scrape_runtime_jobs WHERE runtime_job_id='$PORT_JOB';\""

# Verify staged counts
sudo -u postgres psql -d university_portal -c \
  "SELECT status, auto_publish_status, count(*) FROM scraped_courses WHERE scrape_job_id='$PORT_JOB' GROUP BY 1,2;"
```

**Acceptance:** `imported >= 250`

### Ulster — prod scrape (Batch 1)

```bash
# Ensure ulster_2176.yaml has sitemap_offset: 0 (default) before this run
ULSTER_JOB=$(curl -s -c /tmp/cookie.txt -b /tmp/cookie.txt \
  -X POST http://localhost:8000/api/scrape/start \
  -H "Content-Type: application/json" \
  -d '{"universityId": ULSTER_UNI_ID}' | python3 -c "import sys,json;print(json.load(sys.stdin)['runtimeJobId'])")
echo "Ulster Batch 1 job: $ULSTER_JOB"

# Poll until complete (takes ~35-45 min due to Wayback fallback)
watch -n 60 "sudo -u postgres psql -d university_portal -c \"SELECT status,total_found,imported,errors,heartbeat_at FROM scrape_runtime_jobs WHERE runtime_job_id='$ULSTER_JOB';\""

# Verify staged counts
sudo -u postgres psql -d university_portal -c \
  "SELECT status, auto_publish_status, count(*) FROM scraped_courses WHERE scrape_job_id='$ULSTER_JOB' GROUP BY 1,2;"
```

**Acceptance:** `imported >= 350`

### Ulster — prod scrape (Batch 2, remaining ~487 URLs)

```bash
# Step 1: edit ulster YAML to set sitemap_offset=500
sed -i 's/sitemap_offset: 0/sitemap_offset: 500/' \
  /root/University-and-Course-data/backend-py/scraper_config/unis/ulster_2176.yaml

# Step 2: trigger Batch 2 scrape (no restart needed — YAML is hot-reloaded per job)
ULSTER_JOB2=$(curl -s -c /tmp/cookie.txt -b /tmp/cookie.txt \
  -X POST http://localhost:8000/api/scrape/start \
  -H "Content-Type: application/json" \
  -d '{"universityId": ULSTER_UNI_ID}' | python3 -c "import sys,json;print(json.load(sys.stdin)['runtimeJobId'])")
echo "Ulster Batch 2 job: $ULSTER_JOB2"

# Step 3: wait for completion (poll as above)

# Step 4: RESTORE sitemap_offset to 0 after Batch 2 completes
sed -i 's/sitemap_offset: 500/sitemap_offset: 0/' \
  /root/University-and-Course-data/backend-py/scraper_config/unis/ulster_2176.yaml

# Step 5: verify combined coverage
sudo -u postgres psql -d university_portal -c \
  "SELECT count(DISTINCT source_url) FROM scraped_courses WHERE university_id=ULSTER_UNI_ID AND status='pending';"
```

**Acceptance:** Combined Batch 1 + Batch 2 unique staged URLs ≥ 850 (90% of ~987 catalogue).

### Before/after counts (prod — fill in after running)

| Run | Job ID | imported | notes |
|---|---|---|---|
| Portsmouth | _TBD_ | _TBD_ | |
| Ulster Batch 1 | _TBD_ | _TBD_ | sitemap_offset=0 |
| Ulster Batch 2 | _TBD_ | _TBD_ | sitemap_offset=500 |
| Ulster total unique | — | _TBD_ | combined distinct source_url |

---

## OOM root cause & sitemap_offset implementation

**Root cause (dev):** `max_candidates: 1000` caused 961 courses to be loaded into a single  
`asyncio.gather()`, OOMing at ~50 min with 0 staged.

**Fix:** `max_candidates` capped at 500 (matches `_MAX_COURSES_PER_JOB = 500` in orchestrator).

**New feature — `sitemap_offset` (3 files changed):**

| File | Change |
|---|---|
| `app/services/scraper/config/schema.py` | `DiscoveryConfig.sitemap_offset: Optional[int]` field |
| `app/services/scraper/sitemap.py` | `offset: int = 0` param; slices result list after collection |
| `app/services/scraper/discovery.py` | Passes `sitemap_offset` from config at both discover_from_sitemap call sites |

Because the Ulster sitemap XML is a static file, URL order is deterministic.  
`sitemap_offset=0` always yields courses 1–500; `sitemap_offset=500` yields 501–987.  
No overlapping courses between the two batches.

**Long-term fix (deferred, Task #215):** Batch the `asyncio.gather()` loop in orchestrator.py  
so courses are staged in chunks of N rather than all-at-once, eliminating the OOM risk for  
any university with a large catalogue.
