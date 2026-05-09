"""Tiny SMTP wrapper used by the password-reset flow.

Reads SMTP config from env. If any required setting is missing, falls back
to logging the email body to the application log -- safe for development
and for the "we'll set up SMTP later" case.

Required env (all strings):
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       e.g. 587
    SMTP_USER       login user
    SMTP_PASSWORD   login password / app password
    SMTP_FROM       From: address (defaults to SMTP_USER if missing)

Optional:
    SMTP_USE_TLS    "true"/"false" -- defaults to true on port != 465
"""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("uniportal.email")


def _is_configured() -> bool:
    return all(
        os.environ.get(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")
    )


def _send_sync(to: str, subject: str, body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("SMTP_FROM") or user
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            if use_tls:
                s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)


async def send_email(to: str, subject: str, body: str) -> bool:
    """Send an email asynchronously. Returns True on success.

    When SMTP is not configured, logs the body and returns False (the caller
    can decide whether to surface the link via API for dev convenience).
    """
    if not _is_configured():
        log.warning(
            "SMTP not configured; would have sent to %s\n  Subject: %s\n  Body: %s",
            to,
            subject,
            body,
        )
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
        log.info("Sent email to %s (subject=%r)", to, subject)
        return True
    except Exception as exc:  # noqa: BLE001 -- we intentionally swallow + log
        log.exception("Failed to send email to %s: %s", to, exc)
        return False
