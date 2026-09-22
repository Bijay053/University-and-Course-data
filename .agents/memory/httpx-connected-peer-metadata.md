---
name: HTTPX connected-peer metadata
description: How to validate the actual connected remote address across HTTPX transport backends.
---

When enforcing SSRF protections after connecting with HTTPX/httpcore, read the
network stream's `peername` first and fall back to `server_addr`. Treat the
request as unsafe if neither produces a public IP address.

**Why:** asyncio-backed streams commonly expose `peername`, but the AnyIO stream
used for a real HTTPS connection exposed the public remote endpoint only through
`server_addr`. Requiring `peername` alone falsely rejected a valid official URL.

**How to apply:** Keep the pre-connect DNS/URL checks, redirects disabled, and
proxy environment disabled. After connection, validate the actual endpoint from
either metadata key and retain tests proving public fallback acceptance and
private fallback rejection.