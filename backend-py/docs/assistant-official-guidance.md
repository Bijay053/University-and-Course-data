# Backup official English guidance

Provider YAML files may supply up to two published URLs:

```yaml
research:
  english_guidance_urls:
    - https://www.example.edu/published-english-guidance.pdf.pdf?edition=2026
    - https://www.example.edu/admissions/english
```

This block also works in the existing `scrape_config.admin_config` provider
configuration, through the ordinary configuration merge and validation.
An empty list disables candidates. The LSBU URLs live in its existing
`scraper_config/unis/lsbu_2175.yaml`; no provider registry remains in Python.

URLs must be unique HTTPS URLs on the university's official scrape host
(with or without `www`), without credentials, nonstandard ports, whitespace,
or fragments. Query strings, path case, and repeated `.pdf` suffixes are
preserved. Unknown fields, including numeric course defaults, are rejected.
Use `load_uni_config(..., strict=True)` when validating proposed configuration.

Both assistant engines receive the validated provider configuration. A failed
English-guidance PDF or a failed configured source can offer the remaining
candidates. A successfully read general guidance source stops further fallback
selection. Candidates already checked in the current source set are excluded.
The newer engine also excludes recently checked session-cache entries.

The existing bounded source readers still enforce public-network restrictions,
robots permission, request limits, and parser limits. The legacy engine checks
at most two alternatives within its existing shared research deadline; the newer
engine offers at most two candidates through its ordinary budgeted tools.
Neither rewrites or repairs a failed URL. No alternate transport bypass is added.
Successes use each engine's existing session caching; failures, including HTTP
and parsing failures, are reused for 120 seconds. Explicit fresh research in the
legacy engine retains its existing cache-bypass behavior.

Configuration is not verification. Even a successfully read general English
document is not evidence that a particular course uses its scores. Configured
HTML and PDF guidance are labelled as general research; bulk IELTS proposals
remain blocked until course applicability is established. This feature neither
assigns numeric course values nor publishes records. If all alternatives fail,
the sources remain unavailable and existing course data remains unchanged.
