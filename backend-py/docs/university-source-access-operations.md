# University document access: operational handoff

## Private upload transport

The default upload transport is the authenticated, same-origin portal API, not
a cross-origin browser POST to S3. It uses the existing private bucket and
existing object permissions. Bucket CORS administration is not required.

Prepare an attachment at
`POST /api/scrape/jobs/{job}/assistant/documents/upload-url`, then send the raw
PDF to `POST /api/scrape/jobs/{job}/assistant/documents/content` with the session
cookie, `Content-Type: application/pdf` and the `X-Document-Token` grant header.
The header does not replace authentication. Grants bind staff actor, review,
declared size and expiry. The stream is limited to 2 MB. Writes are conditional,
so a reused grant cannot overwrite bytes already read or included in a preview.
No public ACL is created and storage errors are sanitised.

The legacy presigned form transport is retained for compatibility. Its optional
CORS configuration must not block portal uploads.

Enable `ASSISTANT_DOCUMENT_UPLOADS_ENABLED` only after a real private
write/read/PDF-parse/delete canary succeeds. Verify the signed-in interface
separately: service tests do not establish browser acceptance.

## Status and permission boundary

The PDF matcher foundation was verified in production release
`8efa061f9b8f2839b71040cff65fefc63169e51a`. The shared registry and
single-document error presentation require migration 394. Establish the current
production version from the completed release manifest, exact process identity
and public frontend marker; workspace tests alone do not prove deployment.

Current LSBU document evidence is unavailable. The crawler stops on robots.txt;
this does **not** establish the PDF's HTTP status, document contents, or course
applicability. Search-index snippets are not accepted as update evidence.

## Outbound address

AWS DescribeInstances confirms `13.233.73.176` on the production instance.
Neither that observation nor `IpOwnerId=amazon` establishes an Elastic IP.
DescribeAddresses and DescribeNetworkInterfaces are denied to the available
deployment identity. An administrator should check EC2 → Elastic IP addresses
and the instance's network interface association in ap-south-1.

If no Elastic IP is attached, use an Elastic IP on the existing instance for
this directly egressing host, or a NAT gateway with an Elastic IP if moving
egress to a private subnet. Confirm routing and DNS consequences first: attaching
a different public address can interrupt access and require DNS changes. Verify
the final outbound address before asking LSBU to allowlist it. No networking
changes have been made by this work.

## Exact storage administrator action

Bucket: `scraping-buckets` in AWS account `905043442097`.
Deployment principal: `arn:aws:iam::905043442097:user/replit-production-ssm`.

The deployment identity, application storage identity and EC2 instance role
cannot read bucket CORS. IAM self-grant and role inventory are also denied.
Do not retry those operations until an administrator confirms a change.

An administrator can append this CORS rule directly, preserving every existing
rule and all private-bucket permissions:

```json
{
  "AllowedOrigins": ["https://portal.agentsic.com"],
  "AllowedMethods": ["POST"],
  "AllowedHeaders": ["Content-Type"],
  "MaxAgeSeconds": 300
}
```

Alternatively grant only the following bucket configuration permission to the
deployment identity:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetBucketCORS", "s3:PutBucketCORS"],
    "Resource": "arn:aws:s3:::scraping-buckets"
  }]
}
```

Neither option grants public object access. Keep Block Public Access enabled.
Afterwards test a small signed browser POST, private read, parsing, course
preview, approval fencing, deletion and deletion verification before enabling
production uploads. CORS alone is not proof that object permissions work.

## LSBU authorisation request (draft, not sent)

We operate the Study Info Centre university course-information portal. Please
advise how we can obtain authorised automated access to your published English
language guidance:

https://www.lsbu.ac.uk/_media_old/pdf/guidelines/Eng-lang-requirements.pdf

Our current observed production outbound address is 13.233.73.176 (static
allocation not yet confirmed). User-agent:
StudyInfoCentre-SourceVerifier/1.0. A request to your robots.txt receives HTTP
403 with cf-mitigated: challenge, so our reader stops without requesting the PDF.

Can you provide an approved endpoint or downloadable document, or authorise
this crawler after we confirm the fixed address? Please specify permitted
paths, rates and any authentication requirements. We will respect restrictions
and will not bypass protection challenges.

Official contact starting point: https://www.lsbu.ac.uk/contact-us .
No official crawler API or published crawler authorisation programme has been
verified. Indexed PDF path variants are leads only, not verified alternatives.

## Shared registry management

Apply Alembic migration `394_university_source_access` before starting the new
backend. For local development only:
`PYTHONPATH=. python scripts/apply_migration_394_local.py`.

Authenticated routes:

- `GET /api/universities/{id}/source-access` requires `scraping.view`.
- `PUT /api/universities/{id}/source-access` requires `settings.edit`.

PUT example:

```json
{
  "url": "https://www.example.edu/requirements.pdf",
  "policy": {
    "method": "authorization_required",
    "restrictions": "Awaiting university authorisation",
    "authorization_reference": "University support case reference",
    "alternatives": ["https://www.example.edu/approved-guidance.pdf"]
  }
}
```

Supported methods: official_https, authorized_upload, authorization_required.
Policies never override robots, SSRF validation or protection challenges.
Alternatives must remain on the official owner host, are fetched through the
same guarded reader, and are not automatically treated as course authority.
No unimplemented authenticated API adapter is offered as if it exists.

Policy changes include actor, before/after and timestamp; undo by submitting
the former policy, creating another audit event. Last successful read is kept
separate from the latest failure. No document text, cookies or credentials are
stored in observations. Access failures are shared for 120 seconds, not cached
forever. A previous successful observation is never supplied as document text.
Simultaneous source reads use a transaction-scoped document lock; competing
sessions are told a check is in progress instead of issuing duplicate network
requests. This transient response is not cached as an access failure.

The assistant uses this registry for conversation source reads and document
alternative selection. It is not a replacement for every legacy scraper
transport. Administrators manage it through the API; a dedicated settings UI
is not part of this change.

## Acceptance still required

Do not mark the LSBU workflow complete until an authorised readable document
can be processed against the actual selected cohort (user reported 227 rows).
Verify the live count rather than assuming it is unchanged. Preserve health,
research and other exceptions, preview all five IELTS fields, require approval,
verify writes and audit records, and show unchanged/unmatched/failure counts.
No course values have been inferred from blocked documents.
