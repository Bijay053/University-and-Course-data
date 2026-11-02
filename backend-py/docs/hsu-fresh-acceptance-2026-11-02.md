# Fresh HSU source-to-staging acceptance

## Result

All eight bounded course cases passed using newly fetched official HTML and
the actual extraction/staging code. Five passed in the initial run; a single
provider timeout interrupted the sixth fetch. A continuation fetched and
verified exactly the three remaining cases. No cached/archived source was
substituted for that failed fetch.

Run window: 2026-11-02 06:45:44–06:48:33 UTC.

| Fresh course | International tuition | IELTS checked |
|---|---|---|
| Master of Chiropractic | £17,684 Annual, 2026 | 6.0 overall; all components 5.5 |
| MSc Dietetics (Pre-registration) | £17,684 Annual, 2026 | 7.0 overall; all components 6.5 |
| Research Degree (PhD) | £17,046 Annual, 2026 | 6.5 overall; all components 6.0 |
| MSc Sport and Exercise Rehabilitation | £17,520 Full Course, 2026 | 6.0 overall; all components 5.5 |
| MBA Management with Health Data Analytics and AI | £15,500; period/year remain unknown | Not asserted |
| MSc Global Healthcare Management | £15,000, not bursary-adjusted £12,500; period/year unknown | Not asserted |
| BSc Occupational Therapy | Blank: overseas tuition explicitly unconfirmed | Not asserted |
| MSc Dietetics apprenticeship | Blank: £24,000 is an apprenticeship funding band | 7.0 overall; all components 6.5 |

Every case was committed through `stage_course`, reopened in a different
database session, and checked for final fee tuple, pending status, selected
official evidence matching the asserted fields, and exact durable staged-row
snapshot parity. Negative cases had no selected international-fee evidence.
Official source text was separately inspected for inline international prices,
the implicit undergraduate English wording, the international-only headline,
the unpublished overseas price, and the apprenticeship funding band.

## Isolation and release identity

Persistence used a disposable PostgreSQL cluster with a unique university and
job. The fixture permits real staging commits within a rollback-only outer
transaction, then stops and verifies termination of its owned database.
Production rows were never passed to staging. No course was approved or
published. This was a representative bounded acceptance check, not a full
discovery/Celery catalogue job.

Production remained on `42a4df7cf92080999646f21564017ed419874d87`.
The tested local files matched production byte-for-byte:

| File | SHA-256 |
|---|---|
| `extractors/hsu_facts.py` | `188d685d19f180c28798570aaf5008863612b13fca1f22de34fe45dc3d3662dd` |
| `pipelines/single_course.py` | `bb2901863112e15a39aec4645704c37512531b8cc89a14ae05e06fd9020eae87` |
| `stage_course.py` | `e78411cd7dd8663caa48749320d72916cb890df40b0752a0385d1508fecc250b` |

Before/after read-only production fingerprints were identical:

- 31 review rows: `1c587124cf8c4122d484bacb014bbff811ab59179f945182a1faf00978d97a31`
- 556 evidence rows: `7c9f152c4bc05c88fef84473c1be57752c83fc84d54a4fa921fcaf13a2fd9ad6`
- 93 staged snapshots: `7e10e5915261d2dcd4fb2540587d222fd732f02f3d078c1ff7086a41854a3e73`

SSM read-only receipts: `5636d353-3c9e-4915-9a85-857cff987baa`
and `63e0803e-5129-4f52-9fe5-f01a8dabeedf`.

## Retained source receipts

Full HTML and JSON evidence live in the ignored
`generated-artifacts/hsu-fresh-acceptance/` directory, not deployment or Git.
The initial receipt is `receipt.json`; the continuation is
`60d8c485-857f-4d29-a561-a11998e2b19b/receipt.json`.

Receipt SHA-256:
- Initial: `80eb1b434d4563c16386842bc2e6dd1d1ea2645a38092c44dca7f7db5261500e`
- Continuation: `0c98f30b8dbf41facb78b02cbe4290ae0169781bfa3a1f7cd24d2eb9e63e7299`

Fresh HTML SHA-256, in table order:

1. `393a291c8370a8aedaecd19d797b3071335a9c475da23416c0835f2cd12004ea`
2. `c180b7606fb132ce4984bb01df43ec6b5e7b4a6cca6d6a890ab15c4453ea9297`
3. `042fcb543b6186d64c338eeaee6a113f3c19a3cb708e58002a0ac58fea9cdb11`
4. `9ff8de2ffc13f405908c5b11b603409aa7e67f102de11d3943eb337296a1955f`
5. `0fe265a937f704b2d03c274e3fbeef747ba9283ad3214439dfbf489da6ce32f6`
6. `b760a903f3fb4e34d7368435b910bd9ec23da65733dc720be4d9b398bbb9108c`
7. `cd476c2a5114e712e44d991ad89407914f15f20dc83366ff93b1795b56f52ef4`
8. `981b6b95992f408d69e7a31e2794bb76b0f263fcbc2448a3b402408828f4adba`

## Reproduction and limits

```bash
cd backend-py
RUN_HSU_FRESH_ACCEPTANCE=1 python -m pytest \
  tests/test_hsu_fresh_acceptance.py -q -c /dev/null -p no:cacheprovider
```

An optional comma-separated `HSU_ACCEPTANCE_SLUGS` selects exact remaining
cases for continuation. Each new run writes a unique receipt directory.
Without opt-in the test skips; no paid fetch occurs.

The check disables remote AI enrichment and redundant browser rescue to test
deterministic authority from freshly rendered HTML. It does not certify
optional AI behavior, linked-English recovery, the signed-in UI, or a full
catalogue run. No runtime code was changed or deployed for this task.
