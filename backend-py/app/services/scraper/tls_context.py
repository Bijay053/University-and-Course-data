"""Verified chain completion; never add an independent trust anchor."""
from functools import lru_cache
from pathlib import Path
import ssl

import httpx


@lru_cache(maxsize=1)
def scraper_ssl_context() -> ssl.SSLContext:
    # Preserve HTTPX's normal certifi default and operator SSL_CERT_FILE /
    # SSL_CERT_DIR settings instead of silently replacing configured roots.
    context = httpx.create_ssl_context(verify=True, trust_env=True)
    certificates = Path(__file__).with_name("certs")
    # Official Let's Encrypt issuer certificates downloaded over verified
    # HTTPS from /certs/gen-y/int-yr2.pem and /certs/gen-y/root-yr-by-x1.pem.
    # Essex omits the compatibility path to the already trusted ISRG Root X1.
    # These are chain-building certificates, not new independent trust roots.
    for name in ("letsencrypt-yr2.pem", "letsencrypt-yr-cross-x1.pem"):
        context.load_verify_locations(cafile=str(certificates / name))
    # Require the complete path to an existing self-signed trust anchor.
    # In particular, do NOT allow an imported intermediate to terminate it.
    context.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    return context
