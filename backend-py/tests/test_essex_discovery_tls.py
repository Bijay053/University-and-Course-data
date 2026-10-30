import re
import ssl
import subprocess
from pathlib import Path

import certifi
import pytest

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.discovery import _unwrap_funnelback_redirect
from app.services.scraper.tls_context import scraper_ssl_context


def test_certificate_and_hostname_checks_remain_enabled():
    context = scraper_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert not context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN


def test_operator_trust_configuration_is_not_silently_replaced(monkeypatch, tmp_path):
    scraper_ssl_context.cache_clear()
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-operator-bundle.pem"))
    try:
        with pytest.raises(FileNotFoundError):
            scraper_ssl_context()
    finally:
        scraper_ssl_context.cache_clear()


def test_compatibility_chain_requires_existing_trusted_root():
    certs = Path(__file__).parents[1] / "app/services/scraper/certs"
    result = subprocess.run([
        "openssl", "verify", "-CAfile", certifi.where(),
        "-untrusted", str(certs / "letsencrypt-yr-cross-x1.pem"),
        str(certs / "letsencrypt-yr2.pem"),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    # With only the imported non-self-signed certificates there is no trust
    # anchor. This must fail without OpenSSL's partial-chain option.
    untrusted = subprocess.run([
        "openssl", "verify", "-no-CApath", "-no-CAstore",
        "-CAfile", str(certs / "letsencrypt-yr-cross-x1.pem"),
        str(certs / "letsencrypt-yr2.pem"),
    ], capture_output=True, text=True)
    assert untrusted.returncode != 0


def test_recipe_routes_to_subject_lists_and_excludes_information_pages():
    cfg = load_uni_config(
        slug="essex", name="University of Essex", university_id=103,
        scrape_url="https://www.essex.ac.uk", create_missing_stub=False,
    )
    assert len(cfg.discovery.seed_urls) >= 50
    assert not cfg.discovery.insecure_tls_direct_hostnames
    assert all("/subjects" in url for url in cfg.discovery.seed_urls)
    patterns = cfg.discovery.course_detail_url_patterns
    for path in ("/postgraduate/masters", "/postgraduate/research",
                 "/undergraduate/foundation-years", "/study-abroad"):
        assert not any(re.search(p, "https://www.essex.ac.uk" + path) for p in patterns)
    for path in ("/courses/UG00002/1/BSc-Accounting-and-Finance",
                 "/courses/UG00002/1/BSc-Accounting-and-Finance?startdate=27-10",
                 "/courses/pr00854/2/phd-accounting"):
        assert any(re.search(p, "https://www.essex.ac.uk" + path) for p in patterns)
    redirect = (
        "https://essex-search.funnelback.squiz.cloud/s/redirect?"
        "url=https%3A%2F%2Fwww.essex.ac.uk%2Fcourses%2FUG00002%2F1%2FBSc-Accounting-and-Finance"
    )
    assert any(re.search(p, _unwrap_funnelback_redirect(redirect)) for p in patterns)
