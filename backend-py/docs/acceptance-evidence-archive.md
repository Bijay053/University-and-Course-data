# Fresh-course acceptance evidence archive

`deploy/archive_acceptance_evidence.py` is a standalone, opt-in uploader to the
existing configured S3 bucket. It does not run a scrape, change bucket settings,
delete local evidence, or alter `release-smoke-receipts/v1/` storage/retention.
There is no automatic upload during tests or application startup.

## Safety and inventory

Review the exact files locally before opting in. Only public course-source HTML
and fresh-course test receipt JSON are permitted. Never include environment
dumps, credentials, cookies, signed URLs, private data, or raw operational logs.
The script never reads environment variables into artifacts. AWS credentials use
the existing SDK credential chain and are never included in output.

Create a reviewed inventory with `schema_version: 1`, a safe `acceptance_id`, and
`files: [{"path": "receipt.json", "sha256": "<64 lowercase hex characters>"}]`.
Paths are relative to the evidence directory. List each HTML and receipt
explicitly; there is no recursive directory upload. Only `receipt.json` JSON
names and `.html` files are allowed. Symlinks, traversal, duplicate paths,
hash mismatches, unexpected receipt top-level fields, sensitive JSON keys, and
recognizable secret values fail before upload. Bounds are 100 files, 16 MiB per
file and 128 MiB per inventory. Secret scanning is defense in depth, not proof
that arbitrary files are safe: `--reviewed-safe` confirms human review.
Public form-library `"password":"password"` type labels are not secret values.

The committed `hsu-fresh-acceptance-2026-11-02.inventory.json` pins the original
two receipts and eight HTML files against the published acceptance-report hashes.
The interrupted initial run's receipt is intentionally preserved as-is; archival
does not reinterpret it as a complete run or rewrite its acceptance claims.

## Upload

Use the already configured bucket, its independently confirmed 12-digit owner,
and region. The placeholders below are not new resources or credentials.

```bash
cd /root/University-and-Course-data && python backend-py/deploy/archive_acceptance_evidence.py \
  --upload --reviewed-safe \
  --directory generated-artifacts/hsu-fresh-acceptance \
  --inventory backend-py/docs/hsu-fresh-acceptance-2026-11-02.inventory.json \
  --bucket EXISTING_BUCKET --expected-owner OWNER_ACCOUNT_ID --region BUCKET_REGION
```

Preflight requires all four bucket public-access blocks, enabled versioning, and
no expiration/noncurrent-expiration rule that could affect the archive prefix.
It never changes these settings. Failure leaves local files untouched. Do not
disable bucket safeguards to make an upload succeed.

Required permissions: `s3:GetBucketPublicAccessBlock`, `s3:GetBucketVersioning`,
`s3:GetLifecycleConfiguration`, and prefix-scoped `s3:PutObject`,
`s3:GetObject`, `s3:GetObjectVersion`. No listing, deletion, public ACL,
bucket creation, or bucket mutation is needed.

## Retention proof and recovery

Objects are conditionally created under
`fresh-course-acceptance/v1/<acceptance-id>/<inventory-sha256>/files/`.
An existing object is never overwritten. Every object uses SSE-S3 encryption and
SHA-256 metadata. The uploader downloads the exact returned version and compares
its full bytes, hash metadata, and version identity with the approved local
bytes. A retry verifies existing objects and their exact versions.

Only after every file passes does it create `manifest.json`. The manifest records
each relative path, byte count, SHA-256, S3 key and immutable version ID, plus the
bucket, owner, acceptance identity and inventory digest. The manifest itself is
uploaded conditionally and read back by version before `status: retained` is
printed. Any failure returns exit code 1 and `status: not_retained`; no SDK error
details or evidence contents are printed. Partial uploads are not success;
retry the same inventory without rerunning the acceptance scrape.

Save the success JSON in the durable acceptance report (it contains no
credentials). It gives the manifest key, version ID and hash. To recover after
workspace cleanup, use S3 `GetObject` with that **VersionId** and expected owner,
verify the manifest SHA-256, then fetch each listed file by its exact VersionId
and verify its SHA-256 and byte count. Do not substitute an unversioned latest
object. Serve recovered HTML only as untrusted evidence, not executable app UI.

Version identity prevents an overwrite changing the referenced evidence.
It is not S3 Object Lock/WORM: an administrator with version-deletion permissions
can still delete it. Keep deletion privileges away from the uploader and preserve
the bucket's non-expiring archive policy.

No live upload is implied by committing an inventory or passing offline tests.
Until success JSON is recorded, evidence is still workspace-local.

## Offline verification

```bash
cd /root/University-and-Course-data && cd backend-py && python -m pytest \
  tests/test_archive_acceptance_evidence.py tests/test_archive_smoke_receipts.py \
  tests/test_restore_smoke_receipts.py tests/test_smoke_receipt.py \
  --import-mode=importlib -q -c /dev/null --noconftest -p no:cacheprovider
```
