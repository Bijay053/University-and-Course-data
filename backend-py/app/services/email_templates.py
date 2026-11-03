"""Self-contained authentication emails: inline styles, no remote images or tracking."""
from dataclasses import dataclass
from html import escape
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AuthEmail:
    subject: str
    text: str
    html: str


def _greeting(name: str) -> str:
    return f"Hello {name}," if name else "Hello,"


def _layout(title: str, preheader: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{escape(title)}</title>
<style>@media only screen and (max-width:480px){{.email-padding{{padding:28px 22px!important}}.outer-padding{{padding:20px 12px!important}}.email-title{{font-size:25px!important}}}}</style>
</head>
<body style="margin:0;padding:0;background:#f3f4f7;color:#111827;font-family:Arial,Helvetica,sans-serif">
<div aria-hidden="true" style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all">{escape(preheader)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f4f7">
<tr><td align="center" class="outer-padding" style="padding:40px 16px">
<table role="presentation" width="560" cellspacing="0" cellpadding="0" style="width:100%;max-width:560px;border:1px solid #e3e5eb;border-radius:16px;overflow:hidden;background:#ffffff">
<tr><td style="padding:26px 30px;background:#111526;border-bottom:4px solid #d52f2f">
<table role="presentation" cellspacing="0" cellpadding="0"><tr>
<td width="44" align="center" style="height:44px;background:#d52f2f;border-radius:10px;color:#ffffff;font-size:21px;font-weight:bold">SI</td>
<td style="padding-left:13px;color:#ffffff;font-size:18px;font-weight:bold">Study Info Centre
<div style="margin-top:5px;color:#cbd0de;font-size:10px;font-weight:normal;letter-spacing:2px">ADMIN PORTAL</div></td>
</tr></table></td></tr>
<tr><td class="email-padding" style="padding:34px 32px">
<p style="margin:0 0 12px;color:#b42323;font-size:11px;font-weight:bold;letter-spacing:1.5px">ACCOUNT SECURITY</p>
<h1 class="email-title" style="margin:0 0 22px;font-size:29px;line-height:1.25;color:#111526">{escape(title)}</h1>
{content}
</td></tr>
<tr><td style="padding:22px 30px;background:#f8f9fb;border-top:1px solid #e8eaf0;color:#647084;font-size:12px;line-height:1.7">
<strong style="color:#3c4659">Study Info Centre</strong><br>
This is an automated account-security email. Please do not reply.
</td></tr>
</table>
<p style="margin:20px 0 0;color:#737c8c;font-size:11px;line-height:1.6">Keep your sign-in details private.</p>
</td></tr></table></body></html>"""


def login_code_email(code: str, name: str = "", *, expires_minutes: int = 10) -> AuthEmail:
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("Expected a six-digit verification code")
    if expires_minutes < 1:
        raise ValueError("Verification expiry must be positive")
    greeting = _greeting(name)
    content = f"""
<p style="margin:0 0 14px;font-size:15px;line-height:1.7;color:#465166">{escape(greeting)}</p>
<p style="margin:0 0 24px;font-size:15px;line-height:1.7;color:#465166">Use this verification code to finish signing in to your portal.</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>
<td align="center" style="padding:26px 12px;background:#f6f7fa;border:1px solid #e4e7ee;border-radius:10px">
<div style="margin-bottom:12px;color:#667085;font-size:10px;font-weight:bold;letter-spacing:1.8px">YOUR VERIFICATION CODE</div>
<div style="color:#111526;font-family:Consolas,'Courier New',monospace;font-size:36px;font-weight:bold;letter-spacing:7px;line-height:1.4">{code}</div>
</td></tr></table>
<p style="margin:16px 0 26px;text-align:center;color:#667085;font-size:13px;line-height:1.6">Expires in <strong style="color:#111526">{expires_minutes} minutes</strong> &middot; One-time use only</p>
<p style="margin:0;padding-top:22px;border-top:1px solid #e8eaf0;color:#465166;font-size:13px;line-height:1.7"><strong>Didn&rsquo;t try to sign in?</strong><br>
Do not share this code. Reset your portal password to protect your account.</p>
<p style="margin:16px 0 0;color:#667085;font-size:12px;line-height:1.7">Enter this code only on the portal sign-in page. Never share it by email, phone or chat.</p>"""
    return AuthEmail(
        subject="Your Study Info Centre sign-in code",
        text=f"{greeting}\n\nYour Study Info Centre sign-in verification code is: {code}\n\n"
             f"This code expires in {expires_minutes} minutes and can be used once. "
             "Enter it only on the portal sign-in page.\n\n"
             "If you did not try to sign in, do not share this code. Reset your portal password to protect your account.\n\n"
             "Study Info Centre | Account security\nThis is an automated email. Please do not reply.",
        html=_layout("Verify your sign-in", f"Finish signing in. Your code is valid for {expires_minutes} minutes.", content),
    )


def password_reset_email(url: str, name: str = "", *, expires_hours: int = 2) -> AuthEmail:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Expected an HTTP(S) reset URL")
    if expires_hours < 1:
        raise ValueError("Password reset expiry must be positive")
    safe_url = escape(url, quote=True)
    greeting = _greeting(name)
    content = f"""
<p style="margin:0 0 14px;font-size:15px;line-height:1.7;color:#465166">{escape(greeting)}</p>
<p style="margin:0 0 26px;font-size:15px;line-height:1.7;color:#465166">We received a request to reset your portal password. Select the button below to choose a new one.</p>
<table role="presentation" cellspacing="0" cellpadding="0"><tr><td align="center" style="background:#d52f2f;border-radius:8px">
<a href="{safe_url}" style="display:inline-block;padding:16px 27px;border:1px solid #d52f2f;border-radius:8px;background:#d52f2f;color:#ffffff;font-size:15px;font-weight:bold;line-height:1.2;text-decoration:none">Reset password</a>
</td></tr></table>
<p style="margin:18px 0 26px;color:#667085;font-size:13px;line-height:1.6">This link expires in <strong style="color:#111526">{expires_hours} hours</strong> and can be used once.</p>
<p style="margin:0;padding-top:22px;border-top:1px solid #e8eaf0;color:#465166;font-size:13px;line-height:1.7"><strong>Didn&rsquo;t request a password reset?</strong><br>
You can ignore this email. Requesting a reset does not change your password.</p>
<p style="margin:24px 0 8px;color:#667085;font-size:12px;line-height:1.7">If the button doesn&rsquo;t work, copy and paste this link into your browser:</p>
<p style="margin:0;word-break:break-all;overflow-wrap:anywhere;font-size:12px;line-height:1.7"><a href="{safe_url}" style="color:#b42323;text-decoration:underline">{safe_url}</a></p>
<p style="margin:20px 0 0;color:#667085;font-size:12px;line-height:1.7">Keep this link private. Email verification is still required when you sign in.</p>"""
    return AuthEmail(
        subject="Reset your Study Info Centre password",
        text=f"{greeting}\n\nWe received a request to reset your portal password.\n"
             f"Choose a new password using this link:\n\n{url}\n\n"
             f"This link expires in {expires_hours} hours and can be used once.\n\n"
             "If you did not request this, you can ignore this email. Requesting a reset does not change your password.\n"
             "Keep this link private. Email verification is still required when you sign in.\n\n"
             "Study Info Centre | Account security\nThis is an automated email. Please do not reply.",
        html=_layout("Reset your password", "Choose a new portal password using your secure reset link.", content),
    )
