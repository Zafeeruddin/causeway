"""Signing in, signing out, and getting back in when the password is gone.

Three rules run through this module and are worth stating once rather than at
each handler:

*Say the same thing either way.* A wrong email and a wrong password give the
same 401, and asking for a reset link gives the same 202 whether or not the
address has an account. An endpoint that answers differently is an endpoint
that enumerates your users for anyone who asks politely.

*Count only failures.* Every limit here is on things that went wrong; a
successful sign-in clears the counters. Someone who mistypes twice and then
gets it right never sees a limit.

*Reset is a capability, not a session.* A reset link proves you can read one
mailbox at one moment. It sets a password and stops working; it never signs
anybody in.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import mail
from app.api.auth import (
    COOKIE_NAME,
    current_principal,
    hash_password,
    issue_session,
    require_write,
    verify_password,
)
from app.api.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    Me,
    ResetPasswordRequest,
    TeamBrief,
)
from app.config import Settings, settings
from app.db import Principal, get_session
from app.models import Team, TeamMember, User
from app.security.reset import RESET_TTL_SECONDS, fingerprint, issue_reset, read_reset
from app.security.throttle import Limit, Throttle, client_address

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

#: One counter for the whole process. Shared with the admin routes, which reset
#: passwords through the same door.
throttle = Throttle()


def _limits() -> tuple[Limit, Limit]:
    cfg = settings()
    window = cfg.login_attempt_window_seconds
    return (
        Limit(cfg.login_attempts_per_address, window),
        Limit(cfg.login_attempts_per_account, window),
    )


def _too_many(retry_after: float) -> HTTPException:
    seconds = max(1, int(retry_after + 0.5))
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        f"Too many attempts. Try again in {_spell(seconds)}.",
        headers={"Retry-After": str(seconds)},
    )


def _spell(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = (seconds + 59) // 60
    return "a minute" if minutes == 1 else f"{minutes} minutes"


@router.post("/login", response_model=Me)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> Me:
    cfg = settings()
    email = body.email.lower().strip()
    by_address, by_account = _limits()
    address_key = f"login:ip:{client_address(request, trusted_hops=cfg.trusted_proxy_hops)}"
    account_key = f"login:account:{email}"

    # Checked before the password is verified, so a locked-out caller does not
    # get an argon2 hash computed for them on every attempt -- that is the
    # expensive half, and leaving it reachable turns a rate limiter into a way
    # to spend the server's CPU.
    for key, limit in ((address_key, by_address), (account_key, by_account)):
        wait = throttle.retry_after(key, limit)
        if wait:
            raise _too_many(wait)

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Same message either way: which half was wrong is not the caller's business.
    if user is None or not user.is_active or not verify_password(user.password_hash, body.password):
        throttle.record(address_key, by_address)
        throttle.record(account_key, by_account)
        log.info("auth.login_failed", email=email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email or password is incorrect.")

    throttle.clear(address_key, account_key)
    user.last_login_at = datetime.now(UTC)
    await db.flush()

    _set_session(response, user.id, cfg)
    return await _me(db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me", response_model=Me)
async def me(
    principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_session)
) -> Me:
    user = await db.get(User, principal.user_id)
    assert user is not None
    return await _me(db, user)


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Ask for a reset link. Always accepted, whatever the address.

    The response cannot depend on whether the account exists, so it does not:
    the same 202 and the same sentence come back either way, and everything
    interesting is in the log. When no SMTP is configured nothing is sent at
    all and the sentence stays true, because it only ever promised that *if*
    there is an account, a link is on its way.
    """
    cfg = settings()
    address = client_address(request, trusted_hops=cfg.trusted_proxy_hops)
    limit = Limit(cfg.reset_requests_per_address, cfg.reset_request_window_seconds)
    key = f"reset:ip:{address}"
    wait = throttle.retry_after(key, limit)
    if wait:
        raise _too_many(wait)
    throttle.record(key, limit)

    email = body.email.lower().strip()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        log.info("auth.reset_requested_unknown", email=email, address=address)
    elif not user.may_write:
        # A shared demo login is shared on purpose. Letting anyone holding the
        # published address mail themselves a reset link would hand the account
        # to whoever asked first.
        log.info("auth.reset_refused_demo", email=email, address=address)
    elif not mail.mail_available():
        log.warning("auth.reset_no_smtp", email=email, hint="an administrator must issue the link")
    else:
        link = reset_url(request, issue_reset(user.id, user.password_hash))
        try:
            await mail.send(user.email, "Reset your Causeway password", _reset_body(link))
        except Exception:  # noqa: BLE001 - delivery must not describe the account
            log.exception("auth.reset_send_failed", email=email)

    return {"detail": "If that address has an account, a reset link is on its way."}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    body: ResetPasswordRequest, db: AsyncSession = Depends(get_session)
) -> None:
    """Redeem a reset link. Sets the password; does not sign anybody in.

    Ends at the sign-in page on purpose. A reset that logs you straight in
    turns one readable email into a session, and means a link forwarded by
    accident is an account handed over rather than a password to change again.
    """
    claim = read_reset(body.token)
    if claim is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "This reset link has expired. Ask for another."
        )
    user_id, fp = claim
    user = await db.get(User, user_id)
    if user is None or not user.is_active or not user.may_write:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "This reset link is no longer valid. Ask for another."
        )
    if not _same_fingerprint(fp, user.password_hash):
        # Either it has been redeemed already or a newer link superseded it.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "This reset link has already been used. Ask for another."
        )

    user.password_hash = hash_password(body.password)
    await db.flush()
    throttle.clear(f"login:account:{user.email}")
    log.info("auth.password_reset", user=user.id)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    principal: Principal = Depends(require_write),
    db: AsyncSession = Depends(get_session),
) -> None:
    user = await db.get(User, principal.user_id)
    assert user is not None
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is not your current password.")
    user.password_hash = hash_password(body.password)
    await db.flush()
    log.info("auth.password_changed", user=user.id)


def reset_url(request: Request, token: str) -> str:
    """Where the link points.

    ``PUBLIC_BASE_URL`` when the operator set one, and the request's own origin
    otherwise. Behind a proxy the request's idea of itself is whatever the Host
    header says, which is why the setting exists and why the runbook asks for
    it: a link built from a spoofed Host is a link that sends someone's reset
    token somewhere else.
    """
    base = settings().public_base_url.strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/reset?token={token}"


def _reset_body(link: str) -> str:
    hours = RESET_TTL_SECONDS // 3600
    return (
        "Somebody asked to reset the password on your Causeway account.\n\n"
        f"{link}\n\n"
        f"The link works once and stops working after {hours} hour"
        f"{'s' if hours != 1 else ''}.\n"
        "If this was not you, nothing has changed and you can ignore this.\n"
    )


def _same_fingerprint(claimed: str, password_hash: str) -> bool:
    return hmac.compare_digest(claimed, fingerprint(password_hash))


def _set_session(response: Response, user_id: str, cfg: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issue_session(user_id),
        httponly=True,
        samesite="lax",
        secure=not cfg.is_dev,
        max_age=12 * 3600,
        path="/",
    )


async def _me(db: AsyncSession, user: User) -> Me:
    if user.is_superadmin:
        rows = await db.execute(select(Team))
    else:
        rows = await db.execute(select(Team).join(TeamMember).where(TeamMember.user_id == user.id))
    return Me(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        may_write=user.may_write,
        teams=[TeamBrief.model_validate(t) for t in rows.scalars().all()],
    )
