---
name: Extraction QA metrics script
description: Script to measure 5 extraction failure modes per job or university before expanding to new providers.
---

## Rule
Run `backend-py/scripts/extraction_qa_metrics.py` to get a per-job or per-university breakdown of the 5 extraction failure modes identified from the Teesside scrape analysis.

**Why:** Before expanding to new universities/providers, measure the baseline failure rates so regressions are visible.

**How to apply:**
```bash
# By job:
PYTHONPATH=backend-py python3 backend-py/scripts/extraction_qa_metrics.py --job-id job_XXXXXXXX

# By university:
PYTHONPATH=backend-py python3 backend-py/scripts/extraction_qa_metrics.py --uni-id 2182

# Last 7 days (all universities):
PYTHONPATH=backend-py python3 backend-py/scripts/extraction_qa_metrics.py --days 7
```

**5 failure modes reported:**
1. `scrape_warnings` JSONB breakdown — includes `english_section_detected_scores_blank` and `fee_section_detected_fee_blank`
2. `english_section_detected_scores_blank` by university
3. Marketing-copy location strings (mirrors `_MARKETING_HINTS` from `location.py`)
4. Navigation/promo page titles staged (mirrors `_RE_NAV_PAGE_TITLE` from `guards.py`)
5. Field-miss rates: duration, intake, ielts, fee, campus, academic
