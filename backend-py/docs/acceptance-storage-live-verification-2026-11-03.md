# Live acceptance-evidence storage verification

**Result: passed** at `2026-11-03T01:48:53.433472+00:00`.

This was a real S3/IAM round trip through
`deploy/archive_acceptance_evidence.py`, using synthetic public HTML and a
synthetic receipt. It was not a production upload or an archive of the original
HSU acceptance files. The success references below describe objects verified
during the test and **subsequently deleted during verified teardown**.
This report is an unsigned execution record, not a signed release authorization.

## Isolation and permission scope

- Disposable account: `151955775532`; region: `ap-south-1`.
- Production account `905043442097` was rejected by the harness's account guards.
- Run: `760a11ce00000001`.
- Bucket: `uni-archive-it-151955775532-760a11ce00000001`.
- Stack: `uni-archive-it-760a11ce00000001`.
- Restricted role: `uni-archive-reader-760a11ce00000001`.
- Temporary operator grant: `acceptance-rehearsal-760a11ce00000001`.
- User explicitly approved adding temporary test-only permissions.
- CloudFormation provisioned tagged resources; each mutation group checked
  account identity and run-specific ownership. The operator grant was restricted
  to this bucket/archive prefix and role.
- Bucket had enabled versioning, all four public-access blocks, bucket-owner
  enforced ownership, SSE-S3 encryption, and no expiring archive lifecycle rule.
  Provisioning completed at `2026-11-03T01:32:47.877933+00:00`; the runner waited
  900 seconds for initial versioning propagation.
- The uploader used an assumed role with both identity and session policies
  limited to safety reads and prefix-scoped PutObject/GetObject/GetObjectVersion.
  It had no ListBucket, deletion, or bucket-mutation permission.
- No production credentials, bucket settings, application services, or data were
  used or changed.

## Observed checks

1. Real ListObjectsV2 returned AccessDenied/403.
2. A missing in-prefix GetObject returned AccessDenied/403 without ListBucket.
3. Initial archive created two evidence objects and one manifest.
4. Repeating the same inventory produced three real conditional-write 412
   conflicts, verified the existing objects, returned the same proof, and added
   no object versions.
5. An exact-version manifest download matched its SHA-256; its inventory digest
   matched the original inventory. Each referenced file downloaded by VersionId
   and passed full-byte, hash-metadata, and version-identity verification.
6. An ownership-checked observer wrote a newer corrupt version. The original
   pinned version still verified. The corrupt version and a retry against the
   conflicting current object were rejected.
7. A deliberately lost client response **after a real successful PutObject**
   caused failure; retry completed successfully without duplicating that object.
   This is injected response loss, not a claimed AWS transport outage.
8. Local evidence bytes remained unchanged through successes and failures.
9. Real exact-version deletion, outside-prefix writing, and bucket versioning
   mutation were each denied with AccessDenied/403.
10. The final bucket safety check still passed.

The observed wrapper recorded **5 real conditional conflicts** and **9 pinned
version reads**, in addition to direct manifest/file verification reads.
The seven final object versions (including the intentional corrupt overwrite)
were enumerated and deleted by exact VersionId during cleanup.

## Immutable references verified during the test

Initial inventory SHA-256:
`c86af173c1a0b76bb24862d76fe6fb11afa90b4b85da90a812fda6278c92cf04`

Initial base key:
`fresh-course-acceptance/v1/disposable-roundtrip/c86af173c1a0b76bb24862d76fe6fb11afa90b4b85da90a812fda6278c92cf04/`

| Object relative to base | VersionId | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `files/course.html` | `6yPtk37fzNbYuIgAUHPwY78RXnshg7Rc` | 66 | `896fc2a1521622949d922c4fc1ac207f306510467de3c54ade95d8372269a49c` |
| `files/receipt.json` | `Kr5E59FSyyMseUO0Y6hqo0WPLmWkYybu` | 189 | `571a04076e7b1ac5f7d692086e69f6bc6c389259398b9ee0c931caeb654d529c` |
| `manifest.json` | `dfzyTExPRzJLQ.edNpXqzzdQ3s3PpeVq` | 884 | `cfe389604f3e4e52896170798031b25e47690c74994515bdff6009fa2459478c` |

Lost-response recovery manifest:

- Key: `fresh-course-acceptance/v1/lost-response/135b85f7c0c362e4d585bd121cedea53e53e17eb550f8ccb83673b144d968a6e/manifest.json`
- VersionId: `Aycaj1v4qmgmnbhR8TWRrIrXYD3MPebw`
- SHA-256: `878091445a73c6789d92bdc7127d01c3849ee2e75af51393510376ecd68698ef`
- Bytes: 863; file count: 2; status before teardown: `retained`.

## Cleanup and reproducibility

The runner exited 0 only after exact-version deletion, stack/bucket/role removal,
and temporary grant removal. A separate fresh AWS client session independently
confirmed that the stack, bucket, role, and inline permission grant were absent.
No temporary resources or permission grants were left behind.

Code SHA-256 recorded by the successful run:

- `archive_acceptance_evidence.py`:
  `426b295e742c421103123702ccba7b6f564e8e68da10d003afa1b5a2eb7ceeb0`
- `rehearse_acceptance_evidence.py`:
  `a637b39d38f45a67def06f1893a66e45771225de375d9fb100531b38177da7df`

The raw execution receipt and recovery state were written under the ignored
`generated-artifacts/acceptance-storage/` directory; this report preserves the
non-secret verification record beyond workspace cleanup.

Offline verification also passed: 76 archive/harness tests plus 83 existing
smoke receipt/archive/restore tests. See
[the archive guide](acceptance-evidence-archive.md) for commands and recovery
instructions. These offline tests do not substitute for the live result above.
