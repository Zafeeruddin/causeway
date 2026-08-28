"""Connection profiles: the VPN and jump configuration, and the connect flow."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_principal
from app.api.deps import gateway, load_profile
from app.api.schemas import (
    ConnectResponse,
    GateResultOut,
    ProfileCreate,
    ProfileOut,
    ProfileUpdate,
    TrustRequest,
)
from app.db import Principal, get_session, scoped_select
from app.enums import GateStatus, ProfileState
from app.models import ConnectionProfile, GateRun
from app.security.secrets import secrets_backend
from app.services.audit import record
from app.services.gateway import ConnectionGateway

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


def _out(profile: ConnectionProfile) -> ProfileOut:
    return ProfileOut(
        id=profile.id,
        team_id=profile.team_id,
        name=profile.name,
        mode=profile.mode,
        state=profile.state,
        state_detail=profile.state_detail,
        vpn_kind=profile.vpn_kind,
        vpn_gateway=profile.vpn_gateway,
        vpn_port=profile.vpn_port,
        vpn_username=profile.vpn_username,
        has_vpn_password=bool(profile.vpn_password_ref),
        jump_host=profile.jump_host,
        jump_port=profile.jump_port,
        jump_username=profile.jump_username,
        jump_auth=profile.jump_auth,
        has_jump_credentials=bool(profile.jump_password_ref or profile.jump_key_ref),
        whitelist_url=profile.whitelist_url,
        trusted_cert=profile.trusted_cert,
        trusted_cert_algorithm=profile.trusted_cert_algorithm,
        trusted_cert_accepted_at=profile.trusted_cert_accepted_at,
        tunnel_ip=profile.tunnel_ip,
        last_connected_at=profile.last_connected_at,
    )


@router.get("", response_model=list[ProfileOut])
async def list_profiles(
    db: AsyncSession = Depends(get_session), principal: Principal = Depends(current_principal)
) -> list[ProfileOut]:
    rows = await db.execute(
        scoped_select(ConnectionProfile, principal).order_by(ConnectionProfile.name)
    )
    return [_out(p) for p in rows.scalars().all()]


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(
    body: ProfileCreate,
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ProfileOut:
    if not principal.may_see(body.team_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    secrets = secrets_backend()
    profile = ConnectionProfile(
        team_id=body.team_id,
        name=body.name,
        mode=body.mode,
        vpn_kind=body.vpn_kind,
        vpn_gateway=body.vpn_gateway,
        vpn_port=body.vpn_port,
        vpn_username=body.vpn_username,
        vpn_realm=body.vpn_realm,
        jump_host=body.jump_host,
        jump_port=body.jump_port,
        jump_username=body.jump_username,
        jump_auth=body.jump_auth,
        whitelist_url=body.whitelist_url,
        state=ProfileState.IDLE,
    )
    # Credentials go straight into the secrets backend; the plaintext from the
    # request body is never assigned to a model field.
    if body.vpn_password:
        profile.vpn_password_ref = await secrets.put(body.vpn_password, hint="vpn password")
    if body.wg_config:
        profile.vpn_config_ref = await secrets.put(body.wg_config, hint="wireguard config")
    if body.jump_password:
        profile.jump_password_ref = await secrets.put(body.jump_password, hint="ssh password")
    if body.jump_private_key:
        profile.jump_key_ref = await secrets.put(body.jump_private_key, hint="ssh key")

    db.add(profile)
    await db.flush()
    await record(
        db,
        principal,
        "profile.create",
        "profile",
        profile.id,
        {"mode": str(profile.mode), "vpn_kind": str(profile.vpn_kind)},
        team_id=profile.team_id,
    )
    return _out(profile)


@router.get("/{profile_id}", response_model=ProfileOut)
async def get_profile(profile: ConnectionProfile = Depends(load_profile)) -> ProfileOut:
    return _out(profile)


@router.patch("/{profile_id}", response_model=ProfileOut)
async def update_profile(
    body: ProfileUpdate,
    profile: ConnectionProfile = Depends(load_profile),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> ProfileOut:
    secrets = secrets_backend()
    data = body.model_dump(exclude_unset=True)

    for field, ref_field, hint in (
        ("vpn_password", "vpn_password_ref", "vpn password"),
        ("jump_password", "jump_password_ref", "ssh password"),
        ("jump_private_key", "jump_key_ref", "ssh key"),
    ):
        if field in data:
            value = data.pop(field)
            setattr(profile, ref_field, await secrets.put(value, hint=hint) if value else None)

    for field, value in data.items():
        setattr(profile, field, value)

    # A changed gateway invalidates the pinned certificate: it is a different
    # server now, and silently reusing the old pin would defeat the point.
    if "vpn_gateway" in data and profile.trusted_cert:
        profile.trusted_cert = None
        profile.trusted_cert_accepted_at = None

    await db.flush()
    await record(
        db,
        principal,
        "profile.update",
        "profile",
        profile.id,
        {"fields": sorted(data)},
        team_id=profile.team_id,
    )
    return _out(profile)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile: ConnectionProfile = Depends(load_profile),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
    service: ConnectionGateway = Depends(gateway),
) -> None:
    await service.disconnect(db, profile)
    await db.delete(profile)
    await db.flush()
    await record(db, principal, "profile.delete", "profile", profile.id, team_id=profile.team_id)


@router.post("/{profile_id}/connect", response_model=ConnectResponse)
async def connect(
    profile: ConnectionProfile = Depends(load_profile),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
    service: ConnectionGateway = Depends(gateway),
) -> ConnectResponse:
    """Walk the gates. Every rung is also pushed over the WebSocket as it lands,
    so the dashboard fills in live rather than waiting for this response."""
    outcome = await service.connect(db, profile)
    await record(
        db,
        principal,
        "profile.connect",
        "profile",
        profile.id,
        {"state": str(outcome.state), "attempt_id": outcome.attempt_id},
        team_id=profile.team_id,
    )

    blocked = outcome.blocked_on
    return ConnectResponse(
        attempt_id=outcome.attempt_id,
        state=outcome.state,
        gates=[GateResultOut(**asdict(g)) for g in outcome.results],
        action_required=blocked.detail if blocked else None,
    )


@router.post("/{profile_id}/trust", response_model=ConnectResponse)
async def trust_certificate(
    body: TrustRequest,
    profile: ConnectionProfile = Depends(load_profile),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
    service: ConnectionGateway = Depends(gateway),
) -> ConnectResponse:
    """Accept the gateway's certificate, then retry the connection.

    The desktop client asks this question every time it sees an unknown
    certificate; we ask once and pin the answer to the profile.
    """
    await service.accept_certificate(db, profile, body.fingerprint, principal.user_id)
    await record(
        db,
        principal,
        "profile.trust_certificate",
        "profile",
        profile.id,
        {"fingerprint": body.fingerprint, "gateway": profile.vpn_gateway},
        team_id=profile.team_id,
    )
    return await connect(profile, db, principal, service)


@router.post("/{profile_id}/disconnect", response_model=ProfileOut)
async def disconnect(
    profile: ConnectionProfile = Depends(load_profile),
    db: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
    service: ConnectionGateway = Depends(gateway),
) -> ProfileOut:
    await service.disconnect(db, profile)
    await record(
        db, principal, "profile.disconnect", "profile", profile.id, team_id=profile.team_id
    )
    return _out(profile)


@router.get("/{profile_id}/gates", response_model=list[GateResultOut])
async def latest_gates(
    profile: ConnectionProfile = Depends(load_profile),
    db: AsyncSession = Depends(get_session),
) -> list[GateResultOut]:
    """The most recent attempt, for a page that loaded after the fact."""
    latest = await db.execute(
        select(GateRun.attempt_id)
        .where(GateRun.profile_id == profile.id, GateRun.source_id.is_(None))
        .order_by(GateRun.created_at.desc())
        .limit(1)
    )
    attempt_id = latest.scalar_one_or_none()
    if attempt_id is None:
        return []

    rows = await db.execute(
        select(GateRun).where(GateRun.attempt_id == attempt_id).order_by(GateRun.gate_index)
    )
    return [
        GateResultOut(
            key=r.gate_key,
            index=r.gate_index,
            title=r.gate_key.replace("_", " ").title(),
            status=GateStatus(r.status),
            message=r.message,
            detail=r.detail or {},
            duration_ms=r.duration_ms,
        )
        for r in rows.scalars().all()
    ]
