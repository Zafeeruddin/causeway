"""Sending the one email this system sends.

SMTP is optional and the product works without it: an administrator can hand a
reset link over directly, which is the path that exists on day one of every
deployment and the only one available on an isolated network. When SMTP *is*
configured, self-service reset turns on by itself -- there is no second switch
to forget.

stdlib ``smtplib`` in a worker thread rather than a new async dependency. One
message an hour at the busiest does not justify one.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

import structlog

from app.config import settings

log = structlog.get_logger(__name__)


class MailNotConfigured(RuntimeError):
    """No SMTP host. Callers fall back to the administrator-issued link."""


def mail_available() -> bool:
    cfg = settings()
    return bool(cfg.smtp_host and cfg.smtp_from)


async def send(to: str, subject: str, body: str) -> None:
    """Deliver one plain-text message. Raises rather than swallowing.

    The caller decides what a failure means. For a reset request that is "say
    nothing to the requester and log it", because telling an unauthenticated
    caller that delivery failed tells them the address exists.
    """
    cfg = settings()
    if not mail_available():
        raise MailNotConfigured("SMTP_HOST and SMTP_FROM are not set")

    message = EmailMessage()
    message["From"] = cfg.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    await asyncio.to_thread(_deliver, message)
    log.info("mail.sent", to=to, subject=subject)


def _deliver(message: EmailMessage) -> None:
    cfg = settings()
    factory = smtplib.SMTP_SSL if cfg.smtp_ssl else smtplib.SMTP
    with factory(cfg.smtp_host, cfg.smtp_port, timeout=15) as client:
        if cfg.smtp_starttls and not cfg.smtp_ssl:
            client.starttls()
        if cfg.smtp_username:
            client.login(cfg.smtp_username, cfg.smtp_password)
        client.send_message(message)
