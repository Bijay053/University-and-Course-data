"""Tests for Phase 4A (CMS platform fingerprinting) and Phase 5 (quality intelligence).

Phase 4A: _detect_cms_platform in site_probe.py + _derive_platform_type in auto_config_generator.py
Phase 5:  quality_intelligence.build_quality_report
"""
from __future__ import annotations

import pytest

from app.services.scraper.site_probe import SiteProfile, _detect_cms_platform


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _profile() -> SiteProfile:
    return SiteProfile(url="https://test.edu", probed_at="2026-01-01T00:00:00Z")


# ─── Phase 4A: _detect_cms_platform ───────────────────────────────────────────

class TestDetectCmsPlatform:
    # ── WordPress variants ────────────────────────────────────────────────────

    def test_wordpress_plain(self):
        p = _profile()
        _detect_cms_platform("<html>/wp-content/themes/x/style.css</html>", p)
        assert p.cms_platform == "wordpress"

    def test_wordpress_wp_includes(self):
        p = _profile()
        _detect_cms_platform('<script src="/wp-includes/js/jquery.min.js"></script>', p)
        assert p.cms_platform == "wordpress"

    def test_wordpress_meta_generator(self):
        p = _profile()
        _detect_cms_platform('<meta name="generator" content="WordPress 6.5"/>', p)
        assert p.cms_platform == "wordpress"

    def test_wordpress_elementor(self):
        p = _profile()
        html = '/wp-content/plugins/elementor/assets/js/frontend.min.js class="elementor-widget"'
        _detect_cms_platform(html, p)
        assert p.cms_platform == "wordpress:elementor"

    def test_wordpress_elementor_uppercase(self):
        p = _profile()
        html = '/wp-content/ <div class="Elementor-Section">'
        _detect_cms_platform(html, p)
        assert p.cms_platform == "wordpress:elementor"

    def test_wordpress_divi(self):
        p = _profile()
        _detect_cms_platform('/wp-content/ et-divi-theme et_theme_builder', p)
        assert p.cms_platform == "wordpress:divi"

    def test_wordpress_acf(self):
        p = _profile()
        _detect_cms_platform('/wp-content/ /wp-json/acf/v3/posts/1', p)
        assert p.cms_platform == "wordpress:acf"

    def test_wordpress_acf_field_class(self):
        p = _profile()
        _detect_cms_platform('/wp-content/ class="acf-field acf-field-text"', p)
        assert p.cms_platform == "wordpress:acf"

    # ── Drupal ────────────────────────────────────────────────────────────────

    def test_drupal_settings(self):
        p = _profile()
        _detect_cms_platform("var Drupal = Drupal || {}; Drupal.settings = {};", p)
        assert p.cms_platform == "drupal"

    def test_drupal_sites_default(self):
        p = _profile()
        _detect_cms_platform('/sites/default/files/images/logo.png', p)
        assert p.cms_platform == "drupal"

    def test_drupal_data_attribute(self):
        p = _profile()
        _detect_cms_platform('<div data-drupal-selector="form-field"></div>', p)
        assert p.cms_platform == "drupal"

    def test_drupal_meta_generator(self):
        p = _profile()
        _detect_cms_platform('<meta name="generator" content="Drupal 10"/>', p)
        assert p.cms_platform == "drupal"

    # ── TerminalFour ─────────────────────────────────────────────────────────

    def test_terminalfour_t4tag(self):
        p = _profile()
        _detect_cms_platform('<t4tag type="navigation" name="section link" output="yes" />', p)
        assert p.cms_platform == "terminalfour"

    def test_terminalfour_sitemanager(self):
        p = _profile()
        _detect_cms_platform('/SiteManager/proxy/content/live/', p)
        assert p.cms_platform == "terminalfour"

    def test_terminalfour_name_in_html(self):
        p = _profile()
        _detect_cms_platform('Powered by TerminalFour CMS', p)
        assert p.cms_platform == "terminalfour"

    def test_terminalfour_mediasource(self):
        p = _profile()
        _detect_cms_platform('MediaSourceCMS content management', p)
        assert p.cms_platform == "terminalfour"

    # ── ModernCampus ─────────────────────────────────────────────────────────

    def test_moderncampus_omni(self):
        p = _profile()
        _detect_cms_platform('<link rel="stylesheet" href="/omni-cms/ouglobal.css">', p)
        assert p.cms_platform == "moderncampus"

    def test_moderncampus_oucampus(self):
        p = _profile()
        _detect_cms_platform('{"cms":"oucampus","version":"11"}', p)
        assert p.cms_platform == "moderncampus"

    # ── CourseLeaf ───────────────────────────────────────────────────────────

    def test_courseleaf(self):
        p = _profile()
        _detect_cms_platform('<script src="/courseleaf/js/main.js"></script>', p)
        assert p.cms_platform == "courseleaf"

    def test_courseleaf_leepfrog(self):
        p = _profile()
        _detect_cms_platform('Powered by Leepfrog Technologies', p)
        assert p.cms_platform == "courseleaf"

    def test_courseleaf_class(self):
        p = _profile()
        _detect_cms_platform('<div class="clf-page clf-catalog">', p)
        assert p.cms_platform == "courseleaf"

    # ── Sitecore ─────────────────────────────────────────────────────────────

    def test_sitecore_jssmedia(self):
        p = _profile()
        _detect_cms_platform('src="/-/jssmedia/Project/images/hero.jpg"', p)
        assert p.cms_platform == "sitecore"

    def test_sitecore_context(self):
        p = _profile()
        _detect_cms_platform('window.Sitecore.Context = {"language":"en"}', p)
        assert p.cms_platform == "sitecore"

    # ── SharePoint ───────────────────────────────────────────────────────────

    def test_sharepoint_layouts(self):
        p = _profile()
        _detect_cms_platform('/_layouts/15/sp.init.js?rev=', p)
        assert p.cms_platform == "sharepoint"

    def test_sharepoint_msolayout(self):
        p = _profile()
        _detect_cms_platform('<div id="MSOLayout_InDesignMode">', p)
        assert p.cms_platform == "sharepoint"

    # ── Joomla ───────────────────────────────────────────────────────────────

    def test_joomla_components(self):
        p = _profile()
        _detect_cms_platform('/components/com_content/views/', p)
        assert p.cms_platform == "joomla"

    def test_joomla_meta_generator(self):
        p = _profile()
        _detect_cms_platform('<meta name="generator" content="Joomla! - Open Source Content Management"/>', p)
        assert p.cms_platform == "joomla"

    # ── SilverStripe ─────────────────────────────────────────────────────────

    def test_silverstripe(self):
        p = _profile()
        _detect_cms_platform('SilverStripe\\CMS\\Controllers\\ContentController', p)
        assert p.cms_platform == "silverstripe"

    def test_silverstripe_lowercase(self):
        p = _profile()
        _detect_cms_platform('<!-- silverstripe template -->', p)
        assert p.cms_platform == "silverstripe"

    # ── No CMS detected ──────────────────────────────────────────────────────

    def test_no_cms_generic_html(self):
        p = _profile()
        _detect_cms_platform("<html><body><p>Hello world</p></body></html>", p)
        assert p.cms_platform is None

    def test_no_cms_empty(self):
        p = _profile()
        _detect_cms_platform("", p)
        assert p.cms_platform is None

    def test_no_cms_spa_only(self):
        p = _profile()
        _detect_cms_platform('<div id="root"></div><script src="/static/js/main.chunk.js"></script>', p)
        assert p.cms_platform is None

    # ── Notes side-effect ────────────────────────────────────────────────────

    def test_note_added_when_detected(self):
        p = _profile()
        _detect_cms_platform("/wp-content/uploads/logo.png", p)
        assert any("CMS" in n or "fingerprint" in n for n in p.notes)

    def test_no_note_when_undetected(self):
        p = _profile()
        _detect_cms_platform("<html>nothing</html>", p)
        assert not any("fingerprint" in n for n in p.notes)

    # ── Only first 60 KB is scanned ──────────────────────────────────────────

    def test_beyond_60kb_not_scanned(self):
        """CMS marker placed beyond 60 KB must NOT trigger detection."""
        p = _profile()
        padding = "x" * 62_000
        _detect_cms_platform(padding + "/wp-content/", p)
        assert p.cms_platform is None


# ─── Phase 4A: _derive_platform_type ─────────────────────────────────────────

class TestDerivePlatformType:
    def _make_profile(
        self,
        api_providers=None,
        cms_platform=None,
        situation=None,
        strategy="static_html",
    ):
        """Build a minimal duck-typed SiteProfile for _derive_platform_type."""
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class FakeAPI:
            provider: str

        @dataclass
        class FakeLibStack:
            situation: str

        @dataclass
        class FakeProfile:
            detected_apis: list = dc_field(default_factory=list)
            cms_platform: str | None = None
            library_stack: object = None
            recommended_strategy: str = "static_html"

        p = FakeProfile()
        if api_providers:
            p.detected_apis = [FakeAPI(provider=prov) for prov in api_providers]
        p.cms_platform = cms_platform
        if situation:
            p.library_stack = FakeLibStack(situation=situation)
        p.recommended_strategy = strategy
        return p

    def test_api_provider_wins_over_all(self):
        from app.services.scraper.auto_config_generator import _derive_platform_type
        p = self._make_profile(
            api_providers=["searchstax"],
            cms_platform="wordpress",
            situation="browser_heavy",
        )
        assert _derive_platform_type(p) == "searchstax"

    def test_cms_wins_over_situation(self):
        from app.services.scraper.auto_config_generator import _derive_platform_type
        p = self._make_profile(cms_platform="drupal", situation="static_html_simple")
        assert _derive_platform_type(p) == "drupal"

    def test_cms_wins_over_strategy(self):
        from app.services.scraper.auto_config_generator import _derive_platform_type
        p = self._make_profile(cms_platform="terminalfour", strategy="browser")
        assert _derive_platform_type(p) == "terminalfour"

    def test_situation_used_when_no_cms(self):
        from app.services.scraper.auto_config_generator import _derive_platform_type
        p = self._make_profile(situation="browser_heavy")
        assert _derive_platform_type(p) == "browser_heavy"

    def test_strategy_fallback(self):
        from app.services.scraper.auto_config_generator import _derive_platform_type
        p = self._make_profile(strategy="wayback")
        assert _derive_platform_type(p) == "wayback"

    def test_wordpress_elementor_preserved(self):
        from app.services.scraper.auto_config_generator import _derive_platform_type
        p = self._make_profile(cms_platform="wordpress:elementor")
        assert _derive_platform_type(p) == "wordpress:elementor"

    def test_empty_api_provider_falls_through(self):
        from app.services.scraper.auto_config_generator import _derive_platform_type

        class EmptyAPI:
            provider = ""

        class FakeProf:
            detected_apis = [EmptyAPI()]
            cms_platform = "drupal"
            library_stack = None
            recommended_strategy = "static_html"

        assert _derive_platform_type(FakeProf()) == "drupal"


# ─── Phase 5: quality_intelligence.build_quality_report ──────────────────────

class TestBuildQualityReport:
    from app.services.quality_intelligence import build_quality_report  # noqa: E402

    def _rates(self, **kwargs) -> dict:
        """Build a fill_rates dict from keyword args: field=rate."""
        return {k: {"filled": int(v * 100), "total": 100, "rate": v}
                for k, v in kwargs.items()}

    def test_good_fields_have_no_diagnosis(self):
        from app.services.quality_intelligence import build_quality_report
        rates = self._rates(international_fee=0.95, other_requirement=0.90)
        report = build_quality_report(rates, overall_avg=0.925)
        assert report["fields"]["international_fee"]["status"] == "good"
        assert "diagnosis" not in report["fields"]["international_fee"]
        assert report["issues"] == []

    def test_poor_field_has_diagnosis(self):
        from app.services.quality_intelligence import build_quality_report
        rates = self._rates(other_requirement=0.18)
        report = build_quality_report(rates, overall_avg=0.18)
        field = report["fields"]["other_requirement"]
        assert field["status"] == "poor"
        assert field.get("diagnosis")
        assert field.get("action")

    def test_zero_field_has_zero_diagnosis(self):
        from app.services.quality_intelligence import build_quality_report
        rates = self._rates(international_fee=0.0)
        report = build_quality_report(rates, overall_avg=0.0)
        field = report["fields"]["international_fee"]
        assert field["status"] == "zero"
        assert field.get("diagnosis")
        assert field.get("action")

    def test_warning_field_has_diagnosis(self):
        from app.services.quality_intelligence import build_quality_report
        rates = self._rates(academic_score=0.50)
        report = build_quality_report(rates, overall_avg=0.50)
        field = report["fields"]["academic_score"]
        assert field["status"] == "warning"
        assert field.get("diagnosis")

    def test_overall_pct_computed(self):
        from app.services.quality_intelligence import build_quality_report
        report = build_quality_report(self._rates(), overall_avg=0.68)
        assert report["overall_pct"] == 68

    def test_overall_status_good(self):
        from app.services.quality_intelligence import build_quality_report
        report = build_quality_report(self._rates(), overall_avg=0.85)
        assert report["overall_status"] == "good"

    def test_overall_status_warning(self):
        from app.services.quality_intelligence import build_quality_report
        report = build_quality_report(self._rates(), overall_avg=0.60)
        assert report["overall_status"] == "warning"

    def test_overall_status_poor(self):
        from app.services.quality_intelligence import build_quality_report
        report = build_quality_report(self._rates(), overall_avg=0.20)
        assert report["overall_status"] == "poor"

    def test_overall_status_zero(self):
        from app.services.quality_intelligence import build_quality_report
        report = build_quality_report(self._rates(), overall_avg=0.0)
        assert report["overall_status"] == "zero"

    def test_issues_sorted_critical_first(self):
        from app.services.quality_intelligence import build_quality_report
        rates = self._rates(
            international_fee=0.05,   # critical + poor
            academic_score=0.05,       # non-critical + poor
        )
        report = build_quality_report(rates, overall_avg=0.05)
        assert report["issues"][0]["critical"] is True

    def test_recommended_actions_deduplicated(self):
        from app.services.quality_intelligence import build_quality_report
        # Both fields share the same action in _FIELD_KB
        rates = self._rates(
            other_requirement=0.10,
            degree_level=0.10,
        )
        report = build_quality_report(rates, overall_avg=0.10)
        # Actions should be deduplicated — no duplicates
        actions = report["recommended_actions"]
        assert len(actions) == len(set(actions))

    def test_empty_fill_rates(self):
        from app.services.quality_intelligence import build_quality_report
        report = build_quality_report({}, overall_avg=0.0)
        assert report["fields"] == {}
        assert report["issues"] == []
        assert report["recommended_actions"] == []

    def test_unknown_field_key_no_crash(self):
        from app.services.quality_intelligence import build_quality_report
        rates = {"some_future_field": {"filled": 5, "total": 10, "rate": 0.5}}
        report = build_quality_report(rates, overall_avg=0.5)
        assert "some_future_field" in report["fields"]

    def test_platform_hints_from_probe_summary(self):
        from app.services.quality_intelligence import build_quality_report
        probe = {
            "is_cloudflare_blocked": True,
            "is_js_spa": True,
            "spa_framework": "react",
            "cms_platform": "wordpress:elementor",
            "has_sitemap": False,
            "detected_apis": [],
        }
        report = build_quality_report(self._rates(), probe_summary=probe, overall_avg=0.7)
        hints = report["platform_hints"]
        assert any("Cloudflare" in h for h in hints)
        assert any("SPA" in h for h in hints)
        assert any("wordpress" in h for h in hints)
        assert any("sitemap" in h.lower() for h in hints)

    def test_platform_hints_api_detected(self):
        from app.services.quality_intelligence import build_quality_report
        probe = {
            "is_cloudflare_blocked": False,
            "is_js_spa": False,
            "cms_platform": None,
            "has_sitemap": True,
            "detected_apis": [{"provider": "searchstax"}, {"provider": "algolia"}],
        }
        report = build_quality_report(self._rates(), probe_summary=probe, overall_avg=0.8)
        hints = report["platform_hints"]
        assert any("searchstax" in h.lower() for h in hints)

    def test_no_probe_summary_gives_no_hints(self):
        from app.services.quality_intelligence import build_quality_report
        report = build_quality_report(self._rates(), probe_summary=None, overall_avg=0.7)
        assert report["platform_hints"] == []

    def test_critical_flag_on_international_fee(self):
        from app.services.quality_intelligence import build_quality_report
        rates = self._rates(international_fee=0.10)
        report = build_quality_report(rates, overall_avg=0.10)
        assert report["fields"]["international_fee"]["critical"] is True

    def test_non_critical_flag_on_academic_score(self):
        from app.services.quality_intelligence import build_quality_report
        rates = self._rates(academic_score=0.10)
        report = build_quality_report(rates, overall_avg=0.10)
        assert report["fields"]["academic_score"]["critical"] is False
