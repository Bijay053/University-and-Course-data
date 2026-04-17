# Minimal Browser Automation - Integration Guide

## Overview

This solution uses **minimal browser automation** to handle ONLY the interactive parts (clicking), then hands off to your **existing static HTML extractors**.

### Key Principle

**Browser handles:** Clicking toggles, tabs, accordions
**Your extractors handle:** Everything else (fees, duration, IELTS parsing)

---

## What It Does

### 1. **Click "International" Toggle**
```
[Browser] Loading https://vit.edu.au/mba/mba-project-management
[Browser] Clicking: text="International"
[Browser] Waiting 2s for content update...
✓ Main HTML captured (with international fees)
```

### 2. **Navigate to "Entry Requirements" Tab**
```
[Browser] Clicking: text="Entry Requirements"
[Browser] Waiting 1s for tab to load...
✓ Requirements tab opened
```

### 3. **Expand Collapsed Accordions**
```
[Browser] Expanding: Minimum English Language Requirement
[Browser] Waiting 0.5s...
✓ IELTS content now visible
```

### 4. **Return HTML to Your Extractors**
```python
# Browser returns:
{
    'main_html': '<html>...Fee: $48,000...</html>',
    'requirements_html': '<html>...IELTS: 6.5...</html>',
    'clicks_performed': [
        'international_toggle',
        'requirements_tab',
        'accordions_expanded_1'
    ]
}

# Your existing extractors process it:
fee_data = extract_fee(main_html)  # Your existing function
ielts_data = extract_ielts(requirements_html)  # Your existing function
```

---

## Installation

### Step 1: Install Playwright

```bash
# Install Python package
pip install playwright

# Install Chromium browser
playwright install chromium
```

### Step 2: Add Module to Your Project

```bash
cp minimal_browser_automation.py backend/scraper/browser_helper.py
```

---

## Integration with Existing Scraper

### Option 1: VIT Only (Recommended to Start)

```python
# In your orchestrator.py

from .browser_helper import extract_course_with_browser_assist

async def _extract_complete_course(
    self,
    course_url: str,
    university: University,
    saved_patterns: Optional[Dict] = None
) -> Dict:
    """
    UPDATED: Use browser for VIT, static HTML for others
    """
    
    # ═══════════════════════════════════════════════════════
    # VIT: Use browser automation
    # ═══════════════════════════════════════════════════════
    
    if 'vit.edu.au' in course_url:
        print("[INFO] VIT detected - using browser automation")
        
        # Prepare existing extractors dict
        existing_extractors = {
            'fee_extractor': self.fee_extractor.extract,
            'duration_extractor': self.duration_extractor.extract,
            'study_mode_extractor': self.study_mode_extractor.extract,
            'english_extractor': self.english_extractor.extract,
        }
        
        # Use browser to click, then extract
        course_data = await extract_course_with_browser_assist(
            course_url,
            university.country,
            existing_extractors
        )
        
        return course_data
    
    # ═══════════════════════════════════════════════════════
    # Others: Use existing static HTML approach
    # ═══════════════════════════════════════════════════════
    
    else:
        # Your existing static HTML extraction
        html = await self._fetch_html(course_url)
        
        course_data = {}
        
        for name, extractor in self.extractors.items():
            result = await extractor.extract(html, course_url, university.country)
            course_data.update(result)
        
        return course_data
```

### Option 2: Smart Detection (All Universities)

```python
# In your orchestrator.py

from .browser_helper import extract_course_smart

async def _extract_complete_course(
    self,
    course_url: str,
    university: University,
    saved_patterns: Optional[Dict] = None
) -> Dict:
    """
    UPDATED: Auto-detect when browser is needed
    """
    
    # Prepare extractors
    existing_extractors = {
        'fee_extractor': lambda html, country: self.fee_extractor.extract(html, course_url, country),
        'duration_extractor': lambda html: self.duration_extractor.extract(html, course_url, university.country),
        'study_mode_extractor': lambda html: self.study_mode_extractor.extract(html, course_url, university.country),
        'english_extractor': lambda html: self.english_extractor.extract(html, course_url, university.country),
    }
    
    # Smart extraction (tries static first, uses browser if needed)
    course_data = await extract_course_smart(
        course_url,
        {'country': university.country},
        existing_extractors
    )
    
    return course_data
```

---

## Expected Results - VIT

### Before (Static HTML Only)

```
Courses found: 7 (MBA only)
Fee: $28,000 /Annual ❌
IELTS: Missing ❌
Junk pages: "Vocational", "Course List", "ELICOS" ❌
```

### After (Minimal Browser Automation)

```
[Browser] Loading https://vit.edu.au/mba/mba-project-management
[Browser] Clicking: text="International"
[Browser] Clicking: text="Entry Requirements"
[Browser] Expanding: Minimum English Language Requirement
[Browser] Clicks performed: ['international_toggle', 'requirements_tab', 'accordions_expanded_1']

Courses found: 40+ (all categories)
Fee: $48,000 /Full Course ✓
IELTS: 6.5 (min 6.0) ✓
Junk pages: Filtered ✓
```

---

## Performance

### Timing per Course

- **Static HTML only:** 0.5-1 second
- **Browser automation:** 3-5 seconds

### Optimization

For large universities (40+ courses):

```python
# Process in parallel batches
async def extract_vit_courses(course_urls: List[str]) -> List[Dict]:
    batch_size = 5  # 5 concurrent browsers
    
    for i in range(0, len(course_urls), batch_size):
        batch = course_urls[i:i+batch_size]
        
        # Process batch in parallel
        tasks = [
            extract_course_with_browser_assist(url, 'Australia', extractors)
            for url in batch
        ]
        
        results = await asyncio.gather(*tasks)
        
        # Save results
        for result in results:
            await save_course(result)
```

**VIT (40 courses):**
- Sequential: ~160 seconds (40 × 4s)
- Parallel (5 concurrent): ~32 seconds (8 batches × 4s)

---

## Junk Page Filtering

Add to `research_validator.py`:

```python
self.junk_patterns = [
    # VIT-specific junk
    r'^vocational$',          # Category page
    r'^course\s+list$',       # Navigation page
    r'^elicos$',              # English courses (not degrees)
    r'^bbus$',                # BBus category
    r'^bits$',                # BITS category
    r'^mits$',                # MITS category
    r'^mba$',                 # MBA category
    
    # Generic junk
    r'\bkeydates?$',          # Key dates pages
    r'\binfo\s+night\b',      # Info nights
    r'^double\s+degrees?$',   # Category pages
]
```

---

## Troubleshooting

### Issue: "Chromium not found"

```bash
playwright install chromium
```

### Issue: Browser crashes

Increase timeout:
```python
result = await browser_helper.get_interactive_html(
    url,
    timeout=60000  # 60 seconds
)
```

### Issue: Can't find toggle/tab

Add custom selector:
```python
# In MinimalBrowserHelper.__init__()

self.international_selectors.append(
    '[data-custom-vit-toggle="international"]'
)
```

### Issue: Headless mode not working

Run in headed mode for debugging:
```python
browser = await p.chromium.launch(
    headless=False,  # See what's happening
    slow_mo=1000     # Slow down actions
)
```

---

## Advantages of This Approach

| Aspect | Full Browser Scraper | Minimal Browser Helper | Static HTML Only |
|--------|---------------------|----------------------|------------------|
| **Handles toggles** | ✓ | ✓ | ❌ |
| **Handles tabs** | ✓ | ✓ | ❌ |
| **Speed** | Slow | Medium | Fast |
| **Uses existing extractors** | ❌ (new code) | ✓ (reuse) | ✓ (reuse) |
| **Maintenance** | High | Low | Low |
| **Works for VIT** | ✓ | ✓ | ❌ |

---

## Next Steps

1. **Test with VIT**
   ```bash
   python minimal_browser_automation.py
   ```

2. **Integrate into orchestrator** (Option 1 or 2 above)

3. **Add junk filtering** to validator

4. **Deploy and test** with all VIT courses

5. **Monitor results**:
   - All 40+ courses found?
   - International fees correct?
   - IELTS extracted?
   - No junk pages?

---

## Future: Expand to Other Universities

Once VIT works, you can add more universities that need browser:

```python
# In orchestrator

universities_needing_browser = [
    'vit.edu.au',
    'torrens.edu.au',  # Add if they have toggles
    'other-uni.edu',   # Add as needed
]

if any(domain in course_url for domain in universities_needing_browser):
    # Use browser
    return await extract_course_with_browser_assist(...)
else:
    # Use static HTML
    return await extract_with_static_html(...)
```

---

END OF INTEGRATION GUIDE
