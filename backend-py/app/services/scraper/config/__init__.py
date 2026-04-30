"""Per-university configuration system.

Week-1 deliverable: pure infrastructure, no behaviour change.

Usage
-----
The contextvar is set once at the start of every ``run_scrape`` call.
Extractors and pipeline stages that have been migrated to config-driven
behaviour (Week 2+) call ``get_uni_config()`` to read it.  Unmigrated
code continues to work exactly as before.

Architecture decision: contextvar (not explicit parameter)
----------------------------------------------------------
Every extractor currently takes only the data it needs (HTML, text, etc.).
Threading a ``uni_config`` parameter through all callsites would require a
large mechanical refactor with no behaviour change — and would make the
Week-1 commit very hard to review.

Using a contextvar lets us:
  1. Set the config once per scrape job at the worker entry point.
  2. Migrate extractors one at a time in Week 2+, each reading
     ``get_uni_config()`` as needed.
  3. Keep the change strictly additive in Week 1 (nothing reads the var yet).

Tests that need a specific config can use ``set_uni_config(mock_config)``
before calling the function under test.  No fixtures or monkeypatching needed.
"""
from app.services.scraper.config.schema import (
    UniConfig,
    DiscoveryConfig,
    ExtractionConfig,
    FeesConfig,
    EnglishConfig,
    FiltersConfig,
    TextCleaningConfig,
    StagingConfig,
)
from app.services.scraper.config.context import (
    get_uni_config,
    set_uni_config,
    current_uni_config,
)
from app.services.scraper.config.loader import (
    load_uni_config,
    get_config_for_host,
)

__all__ = [
    "UniConfig",
    "DiscoveryConfig",
    "ExtractionConfig",
    "FeesConfig",
    "EnglishConfig",
    "FiltersConfig",
    "TextCleaningConfig",
    "StagingConfig",
    "get_uni_config",
    "set_uni_config",
    "current_uni_config",
    "load_uni_config",
    "get_config_for_host",
]
