"""Append-only audit trail.

Written on every action that touches a credential, crosses the network boundary,
or moves a recording. Never updated, never deleted by the application -- the
value of the trail is entirely in it being the thing nobody can quietly tidy up.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Principal
from app.models import AuditEvent
from app.security.redaction import redact

log = structlog.get_logger(__name__)


async def record(
    db: AsyncSession,
    principal: Principal | None,
    action: str,
    subject_type: str = "",
    subject_id: str | None = None,
    detail: dict[str, Any] | None = None,
    *,
    team_id: str | None = None,
    source_ip: str | None = None,
    actor_email: str = "",
) -> None:
    db.add(
        AuditEvent(
            team_id=team_id,
            actor_id=principal.user_id if principal else None,
            actor_email=actor_email,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            detail=_clean(detail or {}),
            source_ip=source_ip,
        )
    )
    await db.flush()
    log.info(
        "audit", action=action, subject=subject_id, actor=principal.user_id if principal else None
    )


def _clean(detail: dict[str, Any]) -> dict[str, Any]:
    """Backstop. Credentials should never reach here -- URLs are redacted at
    construction -- but an audit row is a bad place to find out otherwise."""
    return {
        key: redact(value) if isinstance(value, str) else value
        for key, value in detail.items()
        if "password" not in key.lower() and "secret" not in key.lower()
    }
