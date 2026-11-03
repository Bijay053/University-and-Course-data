---
name: Portal login security
description: User requirement for mandatory email verification during portal login.
---

Portal login must require a verification code delivered by email before sign-in
completes.

**Why:** The user requested mandatory OTP and explicitly chose email delivery.

**How to apply:** Preserve mandatory email verification when changing portal
authentication. A password-only sign-in must not bypass this requirement.

Recovery and verification credentials must never be shown in public responses
or application logs, including development fallbacks.

**Why:** The live password-recovery screen exposed the development reset-link
fallback when mail delivery was unconfigured.

**How to apply:** Report globally unavailable mail delivery without checking
whether an account exists. Use the dedicated mail identity and an explicitly
configured SES region and verified sender; do not borrow snapshot-storage
credentials or infer the mail region from the storage region.

Amazon SES SMTP credentials and SES API credentials are not interchangeable.

**Why:** SES rejected the supplied API credential pair while SMTP authentication
and a test email succeeded.

**How to apply:** Keep the mail transport explicit and validate that transport
before enforcing email-dependent authentication. Do not silently switch AWS
identities when mail authentication fails.
