# PRODUCTION DEPLOYMENT GUIDE
# University Course Scraper - Complete Fix Package

## PRE-DEPLOYMENT CHECKLIST

□ Backup existing codebase
□ Review all changes
□ Test on staging environment
□ Verify database migrations (if needed)

---

## STEP 1: BACKUP EXISTING FILES

```bash
# Navigate to backend directory
cd backend/scraper

# Create backup directory
mkdir -p backups/$(date +%Y%m%d_%H%M%S)

# Backup files that will be replaced
cp research_validator.py backups/$(date +%Y%m%d_%H%M%S)/
cp extractors/english_test_extractor.py backups/$(date +%Y%m%d_%H%M%S)/
cp extractors/duration_extractor.py backups/$(date +%Y%m%d_%H%M%S)/
cp orchestrator.py backups/$(date +%Y%m%d_%H%M%S)/
cp validators.py backups/$(date +%Y%m%d_%H%M%S)/ 2>/dev/null || true

echo "✅ Backup complete"
```

---

## STEP 2: DEPLOY NEW FILES

### 2.1 Research Validator
```bash
# Replace research_validator.py
cp /tmp/research_validator.py backend/scraper/research_validator.py
```

**Key Changes:**
- ✅ Lowered validation threshold to 0.40 (from 0.50)
- ✅ Added explicit junk pattern rejection
- ✅ Special case for perfect URL + degree keyword
- ✅ Comprehensive degree keyword list

### 2.2 English Test Extractor
```bash
# Replace english_test_extractor.py
cp /tmp/english_test_extractor.py backend/scraper/extractors/english_test_extractor.py
```

**Key Changes:**
- ✅ Tab/section awareness (finds "Entry Requirements" tabs)
- ✅ HTML table parsing (most reliable)
- ✅ University-level requirements fallback
- ✅ Multi-page extraction
- ✅ All tests: IELTS, PTE, TOEFL, CAE, Duolingo

### 2.3 Duration Extractor
```bash
# Replace duration_extractor.py
cp /tmp/duration_extractor.py backend/scraper/extractors/duration_extractor.py
```

**Key Changes:**
- ✅ Duration validation (prevents "21 Year" errors)
- ✅ Proper "Blended" mode detection (Location: Sydney, Online)
- ✅ Maximum 10 years, minimum 3 months validation

### 2.4 Data Validator
```bash
# Create validators.py if it doesn't exist
cp /tmp/validators.py backend/scraper/validators.py
```

**Key Changes:**
- ✅ Pre-staging data validation
- ✅ Completeness scoring
- ✅ Error detection (unrealistic durations, invalid IELTS scores)

### 2.5 Orchestrator Updates
```bash
# IMPORTANT: Don't replace the entire file
# Merge these updates into your existing orchestrator.py

# Review the changes in /tmp/orchestrator_updates.py
# Then manually update your orchestrator.py with:
# - Updated start_scrape method (complete validation failure handling)
# - Updated continue_after_approval method (junk filtering)
# - New _is_junk_page method
# - Updated _extract_complete_course method (university requirements URL)
```

**Key Changes:**
- ✅ Stop scraping if 0% validation success
- ✅ Post-scrape junk filtering
- ✅ Pass university requirements URL to extractors

---

## STEP 3: VERIFY IMPORTS

Add to `backend/scraper/__init__.py`:

```python
from .research_validator import ResearchValidator
from .validators import DataValidator
from .extractors.english_test_extractor import EnglishTestExtractor
from .extractors.duration_extractor import DurationExtractor

__all__ = [
    'ResearchValidator',
    'DataValidator',
    'EnglishTestExtractor',
    'DurationExtractor',
]
```

---

## STEP 4: DATABASE UPDATES (if needed)

Check if `universities` table needs `requirements_url` field:

```sql
-- Check if column exists
SELECT column_name 
FROM information_schema.columns 
WHERE table_name='universities' 
  AND column_name='requirements_url';

-- Add if missing
ALTER TABLE universities 
ADD COLUMN requirements_url VARCHAR(500);
```

---

## STEP 5: TEST DEPLOYMENT

### Test 1: Small University (ASAHE - 8 courses)
```python
# Test validation
from scraper import ResearchValidator

validator = ResearchValidator()

test_url = 'https://www.asahe.edu.au/courses/bachelor-of-business-international-business'
result = await validator._validate_single_url(test_url)

print(f"Valid: {result['is_valid']}")
print(f"Score: {result['score']}")
print(f"Reason: {result['reason']}")

# Expected: Valid=True, Score>=0.60
```

### Test 2: Medium University (Sydney Met - 12 courses)
```bash
# Trigger scrape via API
curl -X POST http://localhost:8000/api/scrape/start \
  -H "Content-Type: application/json" \
  -d '{
    "university_id": 14,
    "url": "https://sydneymet.edu.au/"
  }'

# Expected results:
# - Research: 10-12/12 samples valid
# - All courses should have IELTS, PTE, TOEFL, CAE
# - Mode should be "Blended" (not "Online")
```

### Test 3: Large University (Torrens - 100+ courses)
```bash
# Trigger scrape
curl -X POST http://localhost:8000/api/scrape/start \
  -H "Content-Type: application/json" \
  -d '{
    "university_id": 5,
    "url": "https://www.torrens.edu.au/"
  }'

# Expected results:
# - Research: 8-10/12 samples valid (not 0/12)
# - Junk pages filtered: MBA Info Night, Double Degrees, etc.
# - 120-125 real courses staged (not 131 with junk)
```

---

## STEP 6: MONITOR LOGS

Watch for these success indicators:

```
✅ [INFO] Found Entry Requirements section
✅ [SUCCESS] Extracted from university-level requirements table
✅ [INFO] Filtered out 5 junk/low-quality pages
✅ Research complete: 10/12 sampled pages are genuine course pages
```

Watch for these error indicators:

```
❌ [CRITICAL] All sample pages rejected
❌ [WARNING] Duration 21 Years (21.0 years) is unrealistic
❌ [ERROR] Gemini API error 404
```

---

## STEP 7: ROLLBACK PLAN (if needed)

If issues occur:

```bash
# Restore from backup
BACKUP_DIR=backups/$(ls -t backups/ | head -1)

cp $BACKUP_DIR/research_validator.py backend/scraper/
cp $BACKUP_DIR/english_test_extractor.py backend/scraper/extractors/
cp $BACKUP_DIR/duration_extractor.py backend/scraper/extractors/
cp $BACKUP_DIR/orchestrator.py backend/scraper/
cp $BACKUP_DIR/validators.py backend/scraper/ 2>/dev/null || true

# Restart services
pm2 restart all
# or
systemctl restart your-app-service
```

---

## EXPECTED IMPROVEMENTS

### Before Fix:
- ❌ Validation: 0/12 samples valid (complete failure)
- ❌ Junk pages: MBA Info Night, Double Degrees staged
- ❌ Missing data: IELTS, PTE, TOEFL, CAE all empty
- ❌ Wrong durations: "21 Year"
- ❌ Wrong modes: "Online" when should be "Blended"

### After Fix:
- ✅ Validation: 8-10/12 samples valid
- ✅ Junk filtering: Event/category pages rejected
- ✅ Complete data: All English tests extracted
- ✅ Validated durations: Realistic ranges only
- ✅ Correct modes: "Blended" when both campus + online

---

## POST-DEPLOYMENT VERIFICATION

Run these checks 24 hours after deployment:

```sql
-- Check completion rates
SELECT 
    university_name,
    COUNT(*) as total_courses,
    COUNT(ielts_overall) as has_ielts,
    COUNT(pte_overall) as has_pte,
    COUNT(duration) as has_duration,
    COUNT(international_fee) as has_fee
FROM courses
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY university_name;

-- Expected: 80%+ completion for all fields
```

---

## SUPPORT

If you encounter issues:

1. Check logs: `tail -f /var/log/scraper.log`
2. Review staged courses: Check for junk pages
3. Test individual extractors in isolation
4. Rollback if critical errors persist

---

## SUCCESS CRITERIA

Deployment is successful when:

✅ Research phase: 70%+ validation success rate
✅ English tests: 90%+ courses have IELTS
✅ Durations: No "21 Year" or unrealistic values
✅ Study modes: Correct "Blended" detection
✅ Junk filtering: Event/news pages rejected
✅ No critical errors in logs

---

END OF DEPLOYMENT GUIDE
