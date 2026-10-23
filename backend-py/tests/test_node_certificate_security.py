"""Offline checks for the October 2026 TLS trust-store update."""
import ssl

import certifi


def test_certifi_version_and_compromised_chain_root_removed():
    assert tuple(map(int, certifi.__version__.split("."))) >= (2026, 10, 19)
    certificates = ssl.create_default_context(cafile=certifi.where()).get_ca_certs()
    common_names = {
        value
        for certificate in certificates
        for rdn in certificate["subject"]
        for key, value in rdn
        if key == "commonName"
    }
    assert "emSign ECC Root CA - G3" not in common_names
    # The targeted fix must not remove unrelated eMudhra trust anchors.
    assert "emSign Root CA - G1" in common_names
