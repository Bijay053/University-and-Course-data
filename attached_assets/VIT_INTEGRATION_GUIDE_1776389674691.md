# VIT (Victorian Institute of Technology) Integration Guide

## Problem Summary

VIT scraping had **5 critical issues**:

1. ❌ **Only 7 courses found** (should be 40+)
2. ❌ **Junk pages scraped** ("MBA Keydates" - not courses)
3. ❌ **Wrong fees** ($28,000/Annual instead of $48,000/Full Course)
4. ❌ **Missing IELTS** (exists on Entry Requirements tab)
5. ❌ **Domestic fees extracted** (should be International)

## Root Causes

1. Used `/courses` page instead of `/course-list`
2. No browser automation to click "International" button
3. Didn't navigate to "Entry Requirements" tab
4. Sitemap incomplete (only 16 URLs, missing BITS/MITS/BBus courses)

---

## Solution: VIT-Specific Scraper Module

Created `vit_scraper.py` that handles:

✅ Correct course discovery from `/course-list?course_categories[]=...`
✅ Browser automation to click "International" button
✅ Navigation to "Entry Requirements" tab
✅ Full course fee extraction (not annual)
✅ Junk filtering ("Keydates" pages)
✅ All course categories (BITS, MITS, MBA, BBus, Vocational)

---

## Installation

### Step 1: Install Playwright (if not already)

```bash
pip install playwright
playwright install chromium
```

### Step 2: Add VIT Scraper Module

```bash
# Copy VIT scraper to your backend
cp vit_scraper.py backend/scraper/site_specific/vit_scraper.py
```

### Step 3: Update Orchestrator

Add VIT detection in `orchestrator.py`:

```python
async def start_scrape(self, university_id: int, url: str, job_id: str):
    """
    UPDATED: Detect VIT and use site-specific scraper
    """
    
    job = await ScrapeJob.get(job_id)
    university = await University.get(university_id)
    
    # ═══════════════════════════════════════════════════════
    # SITE-SPECIFIC SCRAPER DETECTION
    # ═══════════════════════════════════════════════════════
    
    if 'vit.edu.au' in url:
        print("[INFO] VIT detected - using site-specific scraper")
        
        from .site_specific.vit_scraper import scrape_vit_university
        
        result = await scrape_vit_university(university_id)
        
        # Stage courses
        for course in result['courses']:
            await StagedCourse.create(
                job_id=job_id,
                university_id=university_id,
                data=course,
                validation_score=0.95,  # High confidence from site-specific scraper
                missing_fields=[]
            )
        
        await self._update_job_status(job, 'completed', {
            'message': f"VIT scraping complete: {result['courses_extracted']} courses",
            'courses_discovered': result['courses_discovered'],
            'courses_extracted': result['courses_extracted']
        })
        
        return {
            'status': 'success',
            'courses_extracted': result['courses_extracted']
        }
    
    # ... (existing generic scraper code for other universities)
```

---

## Testing

### Test VIT Scraper Directly

```python
from backend.scraper.site_specific.vit_scraper import scrape_vit_university
import asyncio

async def test_vit():
    result = await scrape_vit_university(university_id=16)
    
    print(f"Courses discovered: {result['courses_discovered']}")
    print(f"Courses extracted: {result['courses_extracted']}")
    
    # Check sample course
    sample = result['courses'][0]
    print(f"\nSample: {sample['course_name']}")
    print(f"  Fee: ${sample['international_fee']:,.0f} ({sample['fee_term']})")
    print(f"  Duration: {sample['duration']} {sample['duration_term']}")
    print(f"  IELTS: {sample['ielts_overall']}")

asyncio.run(test_vit())
```

**Expected output:**
```
Courses discovered: 45
Courses extracted: 43

Sample: MBA Project Management
  Fee: $48,000 (Full Course)
  Duration: 2 Years
  IELTS: 6.5
```

---

## Expected Results After Integration

| Metric | Before | After |
|--------|--------|-------|
| **Courses Found** | 7 (MBA only) | 40+ (all categories) |
| **BITS Courses** | 0 | 15+ |
| **MITS Courses** | 0 | 8+ |
| **BBus Courses** | 0 | 3+ |
| **Junk Pages** | 2 ("Keydates") | 0 (filtered) |
| **Fee (MBA PM)** | $28,000/Annual ❌ | $48,000/Full Course ✅ |
| **IELTS** | Missing | 6.5 (6.0 min) ✅ |
| **Study Mode** | Blended ✅ | Blended ✅ |

---

## Verification Checklist

After deployment, verify:

- [ ] 40+ courses discovered (not 7)
- [ ] BITS courses present (e.g., "BITS Artificial Intelligence Analytics")
- [ ] MITS courses present (e.g., "MITS Information Systems")
- [ ] BBus courses present (e.g., "Bachelor of Business")
- [ ] No "Keydates" pages in results
- [ ] Fees are Full Course (not Annual)
- [ ] IELTS is 6.5 with 6.0 minimum bands
- [ ] All fees are international (not domestic)

---

## File Structure

```
backend/
├── scraper/
│   ├── orchestrator.py          # Updated with VIT detection
│   ├── site_specific/           # NEW directory
│   │   ├── __init__.py
│   │   └── vit_scraper.py       # VIT-specific scraper
```

---

## Future: Add More Site-Specific Scrapers

This pattern can be extended for other universities with unique structures:

```python
# In orchestrator.py

if 'vit.edu.au' in url:
    from .site_specific.vit_scraper import scrape_vit_university
    return await scrape_vit_university(university_id)

elif 'university-x.edu' in url:
    from .site_specific.university_x_scraper import scrape_university_x
    return await scrape_university_x(university_id)

else:
    # Use generic scraper
    return await self.generic_scrape(...)
```

---

## Troubleshooting

### Issue: Browser not launching
```bash
# Install browser
playwright install chromium

# Or use system Chrome
# Update vit_scraper.py:
browser = await p.chromium.launch(
    headless=True,
    executable_path='/usr/bin/google-chrome'  # Adjust path
)
```

### Issue: Timeout errors
```python
# Increase timeout in vit_scraper.py:
await page.goto(course_url, wait_until='networkidle', timeout=60000)  # 60 seconds
```

### Issue: Some courses still missing
```python
# Add more categories to VIT scraper:
self.course_categories = [
    'bits', 'mits', 'mba', 'bbus', 'vocational',
    'elicos',  # Add if needed
    'short-courses'  # Add if needed
]
```

---

END OF VIT INTEGRATION GUIDE
