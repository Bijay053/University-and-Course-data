---
name: SSM report completeness
description: Avoid incomplete inventories when collecting production data through AWS command output.
---

AWS SSM command stdout can truncate at roughly 24 KB. Successful command execution does not prove that the entire JSON report was returned.

**Why:** A production inventory and error-log report exceeded the output allowance; the returned JSON was incomplete despite successful execution.

**How to apply:** Filter to necessary fields, page large reports, or gzip/base64 small bounded reports and validate decoding and row counts. Never approve a bulk change or declare an inventory complete using truncated output. Keep credentials and unrelated private data out of reports.
