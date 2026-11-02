# LSBU archive wait budget and guarded release

Deployed revision: `42a4df7cf92080999646f21564017ed419874d87`
Completed 2026-11-02 06:42:19 UTC; independent verification at 06:43:04 UTC.

## Finding and change

The supplied log was from the preceding release and showed repeated 90-second
course deadlines despite disabled redundant browser work. The early Wayback
fallback still waited on a shared single-slot semaphore. Cached replay could
then perform three 25-second requests plus retry sleeps while holding that slot.

LSBU now has a locked 15-second budget for the early archive fallback, including
queue wait, requests, and backoff. Timeout cancels that work and records a
retryable archive failure, then preserves the existing final bounded live
attempt. Other universities retain their existing behavior by default.
No fee, English, delivery, eligibility, or staging rules were relaxed.

## Verification

- 177 focused LSBU, provider-retry, timeout, and guarded-release tests passed.
- Regression proves a stalled archive coroutine is cancelled and the final
  live recovery can still return the course.
- Two live page fetches from the workspace completed in 24.1s and 7.7s:
  Civil Engineering and the legacy Business Management HRM route.
  These are fetch-only observations, not full extraction or catalogue timings.
- Production preflight and post-restart smoke receipts passed.
- API and worker release identities, public health, frontend marker, empty
  active-job counts, and restored scrape consumer passed.
- Operator settings were preserved; no saved LSBU rows were rewritten.
- Local API/worker startup was clean and the sign-in page rendered.
  Signed-in UI was not exercised.

## Receipts

- Release SSM: `fa28a63a-8808-4e31-bc02-a3b2fc3e7309`
- Verification SSM: `bda40092-6154-4828-a847-6d5967b7011b`
- Manifest: `/var/lib/university-portal/release-manifests/release_9c00687ea5124db89cdbe4476d64aaa1/manifest.json`
- Frontend asset: `/assets/index-N1spQoC8.js`
- Private release log/settings backups:
  `/var/lib/university-portal/lsbu-release-20261102/`

The entire 458-candidate scrape was not rerun or timed. This fixes an identified
unbounded archive wait, not a demonstrated end-to-end catalogue latency target.
Provider queue contention, AI extraction time, and historical URL quality still
need measurement in a full-run acceptance check.
