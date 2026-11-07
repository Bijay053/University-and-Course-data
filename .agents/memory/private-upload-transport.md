---
name: Private document upload transport
description: Why private PDF uploads must not depend on bucket CORS administration.
---
Use authenticated same-origin portal uploads by default. Direct browser-to-S3 uploads are optional, not a production prerequisite.

**Why:** The existing application identity can store private documents but cannot administer bucket CORS. Repeatedly requesting CORS access left authorised uploads blocked unnecessarily. Do not broaden IAM permissions or make documents public to solve a browser transport problem.

**How to apply:** Keep actor/review-bound grants, bounded PDF sizes, write-once objects and private reads. Verify real storage write/read/cleanup before enabling uploads. Do not describe backend canaries as signed-in browser acceptance.
