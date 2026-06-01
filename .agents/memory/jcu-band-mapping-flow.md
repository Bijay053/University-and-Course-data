---
name: JCU band_mapping flow fix
description: Why band_mapping must be decoupled from the ielts_missing guard, and how _bm_can_override works.
---

## The Rule
When `band_mapping` is configured for a university, the lookup must run **regardless** of whether `ielts_overall` is already set in the payload — because the central_page always caches the institution's lowest band, but each course page specifies a higher band.

## Why
JCU's Admissions Policy Schedule II has six bands (Band P=5.5 through Band 3c=7.0). The scraper caches Band P (IELTS 5.5) from the central page and writes it to the payload before the band_mapping block runs. The original guard `if payload.get("ielts_overall") in (None, "", 0)` then skips band_mapping entirely because 5.5 ≠ None.

## How to Apply
`_bm_can_override(field_key)` in `single_course.py` (band_mapping block) returns True when:
- The field is blank/None/0, OR
- All existing evidence for that field has `method` containing `"central_page"` or starting with `"yaml_default"`

When `_bm_can_override` is True and a band label is matched, the code:
1. Writes the band-mapped score to `payload[field_key]`
2. Removes the superseded `central_page` evidence entries for that field
3. Appends a new `yaml_band_mapping` evidence entry

## JCU band table (from Admissions Policy Schedule II)
| Band  | IELTS | ielts_each | PTE | TOEFL |
|-------|-------|-----------|-----|-------|
| Band P | 5.5  | 5.0       | 46  | 56    |
| Band 1 | 6.0  | 6.0       | 50  | 74    |
| Band 2 | 6.5  | 6.0       | 58  | 86    |
| Band 3a| 7.0  | 6.5       | 65  | 94    |
| Band 3b| 7.0  | 6.5       | 65  | 94    |
| Band 3c| 7.0  | 7.0       | 65  | 94    |

Config lives in `backend-py/scraper_config/unis/jcu.yaml` under `extraction.english.band_mapping`.
