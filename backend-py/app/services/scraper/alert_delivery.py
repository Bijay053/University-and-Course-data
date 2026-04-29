"""Priority 5 — Component 4: alert delivery.

Persists alerts are already in the DB (done by evaluate_run_alerts).
This module handles out-of-band delivery for critical severity only:
  - Slack webhook (SLACK_WEBHOOK_URL env var)
  - Email via SMTP (ALERT_EMAIL_TO + SMTP_* env vars)

Warnings stay on the dashboard only; this is intentional — only critical
issues that require immediate human action get pushed out.

If neither SLACK_WEBHOOK_URL nor ALERT_EMAIL_TO is set this module is a
no-op, which is fine for environments that use the dashboard exclusively.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
import urllib.error
from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.scrape_run_alert import ScrapeRunAlert

log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL: str | None = os.environ.get("SLACK_WEBHOOK_URL")
ALERT_EMAIL_TO: str | None = os.environ.get("ALERT_EMAIL_TO")
SMTP_HOST: str = os.environ.get("SMTP_HOST", "")
SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER: str | None = os.environ.get("SMTP_USER")
SMTP_PASSWORD: str | None = os.environ.get("SMTP_PASSWORD")
SMTP_FROM: str = os.environ.get("SMTP_FROM", "scraper-alerts@noreply.local")


def format_alert_digest(scrape_run_id: str, alerts: list[ScrapeRunAlert]) -> str:
    lines = [f"Scrape run {scrape_run_id} — {len(alerts)} critical alert(s):\n"]
    for a in alerts:
        lines.append(f"  • [{a.rule_id}] {a.message}")
    lines.append(f"\nReview: check v_university_run_health or scrape_run_alerts where scrape_run_id='{scrape_run_id}'")
    return "\n".join(lines)


async def deliver_alerts(alerts: list[ScrapeRunAlert]) -> None:
    """Send critical alerts to Slack / email.  Warnings stay on dashboard only."""
    critical = [a for a in alerts if a.severity == "critical"]
    if not critical:
        return

    # Group by run ID (normally all from one run, but handle multiple)
    by_run: dict[str, list[ScrapeRunAlert]] = {}
    for a in critical:
        by_run.setdefault(a.scrape_run_id, []).append(a)

    for run_id, run_alerts in by_run.items():
        message = format_alert_digest(run_id, run_alerts)

        if SLACK_WEBHOOK_URL:
            _send_slack(SLACK_WEBHOOK_URL, run_id, message)

        if ALERT_EMAIL_TO and SMTP_HOST:
            _send_email(
                to=ALERT_EMAIL_TO,
                subject=f"[Scraper CRITICAL] {len(run_alerts)} alert(s) in run {run_id}",
                body=message,
            )


# ---------------------------------------------------------------------------
# Transport helpers (sync — acceptable here; delivery is fire-and-forget)
# ---------------------------------------------------------------------------

def _send_slack(webhook_url: str, run_id: str, message: str) -> None:
    payload = json.dumps({"text": message}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
        log.info("[ALERT DELIVERY] Slack OK for run %s (HTTP %s)", run_id, status)
    except urllib.error.URLError as exc:
        log.warning("[ALERT DELIVERY] Slack failed for run %s: %s", run_id, exc)


def _send_email(to: str, subject: str, body: str) -> None:
    if not SMTP_HOST:
        log.debug("[ALERT DELIVERY] SMTP_HOST not set — skipping email")
        return
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            if SMTP_USER and SMTP_PASSWORD:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        log.info("[ALERT DELIVERY] email sent to %s", to)
    except Exception as exc:  # noqa: BLE001
        log.warning("[ALERT DELIVERY] email failed: %s", exc)
