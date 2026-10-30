# Public issuer certificates

These files complete a missing Let's Encrypt chain; they are not secrets.

- `letsencrypt-yr2.pem`: https://letsencrypt.org/certs/gen-y/int-yr2.pem
- `letsencrypt-yr-cross-x1.pem`: https://letsencrypt.org/certs/gen-y/root-yr-by-x1.pem

Retrieved over verified HTTPS on 2026-10-30. The cross-certificate verifies
against the existing ISRG Root X1 trust anchor. The scraper's SSL context keeps
certificate and hostname verification enabled and explicitly disables partial
chain trust, so these non-self-signed certificates cannot become independent
trust anchors. Certifi remains the source of trusted roots (including its
removal of compromised emSign G3).

YR2 expires in September 2028. Revisit this compatibility chain when issuers
rotate; do not replace it with a self-signed root or disable TLS verification.
