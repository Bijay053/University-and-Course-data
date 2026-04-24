"""Unit tests for category-filter listing expansion (T004).

The high-value invariant under test is the host-config split: VIT-only
brand slugs (``bits``, ``mits``, ``mba``, ``bbus``, ``vocational``,
``elicos``) must NOT be probed on non-VIT hosts. Without the split, every
Aussie university that exposes a ``/course-finder``-shaped listing
endpoint would pay ~24 HEAD requests of pure waste per scrape.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.scraper import home_page_redirect


def _run(coro):  # noqa: ANN001 — tiny test helper
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _SlugCapturingClient:
    """Captures every URL handed to ``head()`` for assertion."""

    def __init__(self) -> None:
        self.head_urls: list[str] = []

    async def __aenter__(self) -> "_SlugCapturingClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def head(self, url: str, **_kwargs: Any) -> _FakeResponse:
        self.head_urls.append(url)
        return _FakeResponse(404)  # never harvest — we only care about probes

    async def get(self, url: str, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(404)


def test_slugs_for_host_returns_generic_for_unknown_host() -> None:
    """A host with no entry in ``_HOST_CATEGORY_SLUGS`` gets only the
    generic degree-level slugs."""
    slugs = home_page_redirect._slugs_for_host("csu.edu.au")
    assert slugs == home_page_redirect._GENERIC_CATEGORY_SLUGS
    # Spot-check: VIT-only brand slugs must NOT appear.
    for forbidden in ("bits", "mits", "mba", "bbus", "vocational", "elicos"):
        assert forbidden not in slugs


def test_slugs_for_host_puts_brand_slugs_first() -> None:
    """The vit.edu.au host returns brand slugs FIRST so the 3-empty
    early-exit cannot starve them. Without this ordering, a VIT scrape
    where ``bachelor/master/diploma`` 404 would short-circuit before
    ``bbus/mits/mba`` ever got probed and the 24 → 30 expansion would
    silently fail (architect-flagged regression in 260f824)."""
    slugs = home_page_redirect._slugs_for_host("vit.edu.au")
    brand = ("bits", "mits", "mba", "bbus", "vocational", "elicos")
    # Brand slugs occupy positions 0..len(brand)-1.
    assert slugs[: len(brand)] == brand
    # Generic slugs come AFTER the brand slugs.
    assert slugs[len(brand):] == home_page_redirect._GENERIC_CATEGORY_SLUGS


def test_slugs_for_host_is_case_insensitive() -> None:
    """A capitalised host (``VIT.edu.au``) still hits the VIT entry."""
    slugs = home_page_redirect._slugs_for_host("VIT.edu.au")
    assert "bits" in slugs


def test_slugs_for_host_strips_www_prefix() -> None:
    """Real VIT URLs often resolve via ``www.vit.edu.au`` — that must
    still hit the host-specific slug list."""
    assert "bits" in home_page_redirect._slugs_for_host("www.vit.edu.au")


def test_slugs_for_host_strips_port() -> None:
    """A host with an explicit port (``vit.edu.au:443``) still matches
    the bare-host dict key."""
    assert "bits" in home_page_redirect._slugs_for_host("vit.edu.au:443")


def test_slugs_for_host_strips_user_info() -> None:
    """Pathological but defensive — user-info on the netloc shouldn't
    break host matching."""
    assert "bits" in home_page_redirect._slugs_for_host("user:pass@vit.edu.au")


def test_normalise_host_examples() -> None:
    """Direct unit coverage of the normaliser used by _slugs_for_host."""
    n = home_page_redirect._normalise_host
    assert n("vit.edu.au") == "vit.edu.au"
    assert n("VIT.EDU.AU") == "vit.edu.au"
    assert n("www.vit.edu.au") == "vit.edu.au"
    assert n("vit.edu.au:443") == "vit.edu.au"
    assert n("WWW.vit.edu.au:8080") == "vit.edu.au"
    assert n("u:p@www.vit.edu.au:443") == "vit.edu.au"
    assert n("") == ""
    assert n(None) == ""  # type: ignore[arg-type]


def test_expand_does_not_leak_vit_slugs_to_csu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The most important assertion — when ``listing_url`` is on a non-
    VIT host, no HEAD probe URL contains a VIT-only brand slug.

    Currently CSU does not expose a ``/course-finder`` path, so this
    test uses a synthetic CSU URL on ``/course-finder`` to exercise the
    expansion code path. The point is the slug-leak invariant: even on
    a non-VIT host that *does* use a category-filter listing, we don't
    waste HEAD probes on VIT-specific brand slugs.
    """
    captor = _SlugCapturingClient()
    monkeypatch.setattr(
        home_page_redirect.httpx, "AsyncClient", lambda *_a, **_kw: captor
    )

    _run(
        home_page_redirect.expand_course_list_with_categories(
            "https://study.csu.edu.au/course-finder", []
        )
    )

    forbidden_slugs = ("bits", "mits", "mba", "bbus", "vocational", "elicos")
    for url in captor.head_urls:
        for slug in forbidden_slugs:
            # Match each forbidden slug both as a query value and as a
            # path component, e.g. "?course_categories[0]=bits" and
            # "/course-finder/bits".
            assert f"={slug}" not in url, (
                f"VIT slug {slug!r} leaked to CSU HEAD probe {url!r}"
            )
            assert not url.endswith(f"/{slug}"), (
                f"VIT slug {slug!r} leaked to CSU HEAD probe {url!r}"
            )


def test_expand_probes_vit_brand_slugs_on_vit_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterpart to the leak test — on the VIT host, brand slugs
    *should* be probed (otherwise T004 doesn't bring 24 → 30)."""
    captor = _SlugCapturingClient()
    monkeypatch.setattr(
        home_page_redirect.httpx, "AsyncClient", lambda *_a, **_kw: captor
    )
    # Disable early-exit for this one test — we want every slug attempted
    # so we can confirm the brand slugs were enqueued at all.
    monkeypatch.setattr(home_page_redirect, "_CATEGORY_EXPAND_EARLY_EXIT", 999)

    _run(
        home_page_redirect.expand_course_list_with_categories(
            "https://vit.edu.au/course-list", []
        )
    )

    # At least one HEAD URL must reference each brand slug.
    head_blob = " ".join(captor.head_urls)
    for required in ("bits", "mits", "mba", "bbus"):
        assert required in head_blob, (
            f"VIT brand slug {required!r} was not probed on the VIT host"
        )


def test_generic_slug_list_excludes_vit_brand_slugs() -> None:
    """Defence-in-depth — direct assertion on the constant so a future
    refactor that accidentally moves a brand slug into the generic set
    is caught immediately."""
    for forbidden in ("bits", "mits", "mba", "bbus", "vocational", "elicos"):
        assert forbidden not in home_page_redirect._GENERIC_CATEGORY_SLUGS
