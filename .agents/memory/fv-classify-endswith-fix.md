---
name: _fv_classify and _classify_url_type listing hint endswith-only fix
description: Listing hint matching must use endswith, not substring, or mid-path hints (e.g. /study, /programmes) misclassify individual course pages as "listing".
---

## Rule

Both `_classify_url_type` (in `post_test_discovery`) and `_fv_classify` (in `post_full_validation`) classify URLs as "listing" using a hint list. The hints must be checked with `endswith` ONLY — not substring `h in lurl`.

## Why

Lincoln University course pages follow the pattern:
`/study/study-programmes/programme-search/<course-slug>/`

The hint `/study` appears mid-path → substring check `"/study" in lurl` → True → classified "listing".
The hint `/programmes` appears in "study-programmes" → same problem.

Using `endswith` correctly identifies listing pages (their path terminates at the hub word) vs course pages (their path continues with a specific degree slug).

## How to apply

- `_LISTING_HINTS_TD` (test discovery): `lurl.endswith(h.rstrip("/"))` only
- `_LISTING_P_FV` (full validation): `lurl.rstrip("/").endswith(h.rstrip("/"))` only
- New listing hints to add to `_LISTING_P_FV`: `/programme-search`, `/course-search`, `/study-programmes`, `/undergraduate-study`, `/postgraduate-study` — these are hub pages when the path terminates there

## Opposite case

URLs that ARE genuine listing pages (will match endswith correctly):
- `https://uni.ac.nz/study` → ends with `/study` → listing ✅
- `https://uni.ac.nz/courses` → ends with `/courses` → listing ✅
- `https://uni.ac.nz/study/study-programmes/programme-search/` → ends with `/programme-search` → listing ✅
- `https://uni.ac.nz/study/study-programmes/programme-search/doctor-of-phd/` → ends with course slug → course ✅
