"""Load and merge per-university scraper configuration.

Priority chain (lowest → highest):
  1. Built-in Pydantic defaults (schema.py field defaults)
  2. ``scraper_config/defaults.yaml``            — conservative global defaults
  3. ``scraper_config/unis/<slug>.yaml``          — per-university overrides
  4. Relevant fields from DB ``university.scrape_config`` (backwards compat)

The loader is intentionally synchronous: it is called once per scrape job
at worker startup, before any async I/O.  Results are NOT cached at the
module level so that config file changes take effect on the next scrape
without requiring a worker restart.  If the startup cost becomes measurable
(unlikely given ~20 YAML files), add an LRU cache keyed on (slug, mtime).

Slug derivation
---------------
The slug is derived from the primary hostname of the university's scrape URL:

  www.acu.edu.au  →  acu
  www.aut.ac.nz   →  aut
  bond.edu.au     →  bond

The per-university YAML file is looked up at ``unis/<slug>.yaml``.  If no
file exists for that slug the loader falls back to a config built from
defaults only (plus the DB scrape_config backwards-compat translation).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from app.services.scraper.config.schema import UniConfig

log = logging.getLogger(__name__)

_CONFIGS_ROOT = Path(__file__).parent.parent.parent.parent.parent / "scraper_config"
_DEFAULTS_FILE = _CONFIGS_ROOT / "defaults.yaml"
_UNIS_DIR = _CONFIGS_ROOT / "unis"

# Common TLD-style tokens that should not become the slug.
_TLD_TOKENS: frozenset[str] = frozenset(
    {"edu", "ac", "com", "net", "org", "gov", "au", "nz", "uk", "us", "ca"}
)


def _hostname_to_slug(hostname: str) -> str:
    """Derive a short slug from a hostname.

    >>> _hostname_to_slug("www.acu.edu.au")
    'acu'
    >>> _hostname_to_slug("www.aut.ac.nz")
    'aut'
    >>> _hostname_to_slug("bond.edu.au")
    'bond'
    >>> _hostname_to_slug("www.uow.edu.au")
    'uow'
    """
    h = hostname.lower().removeprefix("www.")
    parts = h.split(".")
    for part in parts:
        if part not in _TLD_TOKENS:
            return part
    return parts[0]


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict.  Returns {} on error."""
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as exc:
        log.error("YAML parse error in %s: %s", path, exc)
        return {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*.  Override wins on conflicts."""
    result: dict[str, Any] = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _translate_db_scrape_config(db_cfg: dict[str, Any]) -> dict[str, Any]:
    """Translate the legacy DB ``university.scrape_config`` JSONB into the new schema.

    The existing ``uniPages`` keys in the DB are the source-of-truth for
    central-page URLs that have been manually configured through the Admin UI.
    We translate them into the new ``extraction.fees`` / ``extraction.english``
    structure so that the UniConfig reflects the full picture.

    Precedence: per-uni YAML > DB scrape_config.  The caller applies the DB
    translation BEFORE the per-uni YAML so that an explicit YAML entry wins.

    This translation is READ-ONLY — it never modifies the DB.
    """
    uni_pages: dict[str, str] = db_cfg.get("uniPages") or {}
    translated: dict[str, Any] = {}

    fees_central = uni_pages.get("feePage") or uni_pages.get("feesPage")
    fees_pdf = uni_pages.get("feesPdf")
    if fees_central:
        translated.setdefault("extraction", {}).setdefault("fees", {})["central_page"] = (
            fees_central
        )
    if fees_pdf:
        translated.setdefault("extraction", {}).setdefault("fees", {})["fees_pdf_url"] = (
            fees_pdf
        )

    english_page = (
        uni_pages.get("entryPage")
        or uni_pages.get("requirementsPage")
        or uni_pages.get("englishPage")
    )
    english_pdf = uni_pages.get("requirementsPdf")
    if english_page:
        translated.setdefault("extraction", {}).setdefault("english", {})["central_page"] = (
            english_page
        )
    if english_pdf:
        translated.setdefault("extraction", {}).setdefault("english", {})[
            "requirements_pdf_url"
        ] = english_pdf

    return translated


def load_uni_config(
    *,
    slug: str,
    name: str,
    scrape_url: str,
    base_url: str = "",
    university_id: int | None = None,
    db_scrape_config: dict[str, Any] | None = None,
) -> UniConfig:
    """Build a fully-merged UniConfig for one university.

    Parameters
    ----------
    slug:
        Short identifier for the university (e.g. ``"acu"``).  Used to look
        up ``scraper_config/unis/<slug>.yaml``.
    name:
        Human-readable name (e.g. ``"Australian Catholic University"``).
    scrape_url:
        Discovery entry-point URL (e.g. ``"https://www.acu.edu.au/courses"``).
    base_url:
        Origin URL.  If omitted, derived from ``scrape_url``.
    university_id:
        DB primary key.  Stored in the config for logging only.
    db_scrape_config:
        Contents of ``university.scrape_config`` from the DB.  Translated
        into the new schema and merged at lower priority than the YAML file.
    """
    if not base_url:
        parsed = urlparse(scrape_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else scrape_url

    # 1. Load defaults.yaml
    merged: dict[str, Any] = _load_yaml_file(_DEFAULTS_FILE)

    # 2. Translate DB scrape_config (lower priority than YAML)
    if db_scrape_config:
        db_translated = _translate_db_scrape_config(db_scrape_config)
        merged = _deep_merge(merged, db_translated)

    # 3. Load and merge per-uni YAML (highest config-file priority)
    uni_yaml_path = _UNIS_DIR / f"{slug}.yaml"
    per_uni = _load_yaml_file(uni_yaml_path)
    if per_uni:
        merged = _deep_merge(merged, per_uni)
    else:
        log.debug("No per-uni YAML for slug=%r (will use defaults + DB config)", slug)

    # 4. Inject identity fields (these are not in YAML, they come from the DB row)
    merged.pop("slug", None)
    merged.pop("name", None)
    merged.pop("university_id", None)
    merged.pop("base_url", None)
    merged.pop("scrape_url", None)

    try:
        return UniConfig(
            slug=slug,
            name=name,
            university_id=university_id,
            base_url=base_url,
            scrape_url=scrape_url,
            **merged,
        )
    except Exception as exc:
        log.error(
            "Failed to build UniConfig for slug=%r: %s — falling back to bare defaults",
            slug,
            exc,
        )
        return UniConfig(
            slug=slug,
            name=name,
            university_id=university_id,
            base_url=base_url,
            scrape_url=scrape_url,
        )


def get_config_for_host(
    *,
    hostname: str,
    name: str,
    scrape_url: str,
    university_id: int | None = None,
    db_scrape_config: dict[str, Any] | None = None,
) -> UniConfig:
    """Convenience wrapper: derive slug from hostname then call ``load_uni_config``."""
    slug = _hostname_to_slug(hostname)
    return load_uni_config(
        slug=slug,
        name=name,
        scrape_url=scrape_url,
        university_id=university_id,
        db_scrape_config=db_scrape_config,
    )
