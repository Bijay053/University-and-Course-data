"""Review-time services: cross-evidence conflict detection, etc.

Kept as a package separate from ``app.services.scraper`` because review
runs are post-staging and may be re-triggered independently (e.g. when an
operator adds a new evidence row through the modal).
"""
