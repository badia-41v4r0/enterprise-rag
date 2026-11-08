"""Admin API: users, roles, containers, grants, audit. Every route requires `is_admin`."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_admin
from app.core.db import get_db
from app.core.security import hash_password
from app.models import (
    Action,
    AuditLog,
    Classification,
    Container,
    Document,
    Grant,
    ResourceType,
    Role,
    Session,
    Tool,
    User,
    UserRole,
)
from app.services.audit import audit

router = APIRouter(prefix="/api/admin", tags=["admin"])

# --- Schemas --------------------------------------------------------------------------------------


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_admin: bool
    is_active: bool
    role_ids: list[uuid.UUID]


class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="", max_length=200)
    password: str = Field(min_length=12, max_length=1024)
    is_admin: bool = False
    role_ids: list[uuid.UUID] = Field(default_factory=list)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, min_length=12, max_length=1024)
    is_admin: bool | None = None
    is_active: bool | None = None
    role_ids: list[uuid.UUID] | None = None


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str


class ContainerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=100)
    description: str = ""
    classification: Classification


class ContainerPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    classification: Classification | None = None


class ContainerOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str
    classification: Classification


class GrantIn(BaseModel):
    role_id: uuid.UUID
    resource_type: ResourceType
    resource_id: uuid.UUID
    action: Action


class GrantOut(GrantIn):
    id: uuid.UUID


class AuditOut(BaseModel):
    id: int
    ts: datetime
    user_id: uuid.UUID | None
    action: str
    resource: str
    decision: str
    details: dict[str, Any]


VALID_GRANTS = {
    ResourceType.container: {Action.read, Action.manage},
    ResourceType.document: {Action.read},
    ResourceType.tool: {Action.execute},
}


def _user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        is_admin=u.is_admin,
        is_active=u.is_active,
        role_ids=sorted((r.id for r in u.roles), key=str),
    )


def _container_out(c: Container) -> ContainerOut:
    return ContainerOut(id=c.id, name=c.name, slug=c.slug, description=c.description, classification=c.classification)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:100] or "container"


async def _set_roles(db: AsyncSession, user_id: uuid.UUID, role_ids: list[uuid.UUID]) -> None:
    if role_ids:
        found = set((await db.scalars(select(Role.id).where(Role.id.in_(role_ids)))).all())
        if found != set(role_ids):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown role id")
    await db.execute(delete(UserRole).where(UserRole.user_id == user_id))
    for rid in set(role_ids):
        db.add(UserRole(user_id=user_id, role_id=rid))


# --- Users ----------------------------------------------------------------------------------------


@router.get("/users")
async def list_users(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[UserOut]:
    users = (await db.scalars(select(User).order_by(User.email))).all()
    return [_user_out(u) for u in users]


@router.post("/users", status_code=201)
async def create_user(
    body: UserIn, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> UserOut:
    user = User(
        email=body.email.strip().lower(),
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        is_admin=body.is_admin,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already exists") from exc
    await _set_roles(db, user.id, body.role_ids)
    audit(db, "admin.user.create", user_id=auth.user.id, resource=f"user:{user.id}", details={"email": user.email})
    await db.commit()
    await db.refresh(user, ["roles"])
    return _user_out(user)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID, body: UserPatch, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> UserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    changes: dict[str, Any] = {}
    if body.display_name is not None:
        user.display_name = body.display_name
        changes["display_name"] = body.display_name
    if body.password is not None:
        user.password_hash = hash_password(body.password)
        changes["password"] = "changed"  # noqa: S105 - audit marker, not a secret
    if body.is_admin is not None:
        if user.id == auth.user.id and not body.is_admin:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot remove your own admin flag")
        user.is_admin = body.is_admin
        changes["is_admin"] = body.is_admin
    if body.is_active is not None:
        if user.id == auth.user.id and not body.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot deactivate yourself")
        user.is_active = body.is_active
        changes["is_active"] = body.is_active
        if not body.is_active:
            await db.execute(delete(Session).where(Session.user_id == user.id))
    if body.role_ids is not None:
        await _set_roles(db, user.id, body.role_ids)
        changes["role_ids"] = [str(r) for r in body.role_ids]
    audit(db, "admin.user.update", user_id=auth.user.id, resource=f"user:{user.id}", details=changes)
    await db.commit()
    await db.refresh(user, ["roles"])
    return _user_out(user)


# --- Roles ----------------------------------------------------------------------------------------


@router.get("/roles")
async def list_roles(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[RoleOut]:
    roles = (await db.scalars(select(Role).order_by(Role.name))).all()
    return [RoleOut(id=r.id, name=r.name, description=r.description) for r in roles]


@router.post("/roles", status_code=201)
async def create_role(
    body: RoleIn, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> RoleOut:
    role = Role(name=body.name, description=body.description)
    db.add(role)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Role already exists") from exc
    audit(db, "admin.role.create", user_id=auth.user.id, resource=f"role:{role.id}", details={"name": role.name})
    await db.commit()
    return RoleOut(id=role.id, name=role.name, description=role.description)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> None:
    role = await db.get(Role, role_id)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")
    await db.delete(role)
    audit(db, "admin.role.delete", user_id=auth.user.id, resource=f"role:{role_id}", details={"name": role.name})
    await db.commit()


# --- Containers -----------------------------------------------------------------------------------


@router.get("/containers")
async def list_containers(
    _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[ContainerOut]:
    return [_container_out(c) for c in (await db.scalars(select(Container).order_by(Container.name))).all()]


@router.post("/containers", status_code=201)
async def create_container(
    body: ContainerIn, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> ContainerOut:
    c = Container(
        name=body.name,
        slug=slugify(body.slug or body.name),
        description=body.description,
        classification=body.classification,
    )
    db.add(c)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Container name or slug already exists") from exc
    audit(
        db,
        "admin.container.create",
        user_id=auth.user.id,
        resource=f"container:{c.id}",
        details={"name": c.name, "classification": c.classification.value},
    )
    await db.commit()
    return _container_out(c)


@router.patch("/containers/{container_id}")
async def update_container(
    container_id: uuid.UUID,
    body: ContainerPatch,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ContainerOut:
    c = await db.get(Container, container_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Container not found")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(c, k, v)
    audit(db, "admin.container.update", user_id=auth.user.id, resource=f"container:{c.id}", details=changes)
    await db.commit()
    return _container_out(c)


@router.delete("/containers/{container_id}", status_code=204)
async def delete_container(
    container_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> None:
    c = await db.get(Container, container_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Container not found")
    await db.execute(
        delete(Grant).where(Grant.resource_type == ResourceType.container, Grant.resource_id == container_id)
    )
    await db.delete(c)
    audit(db, "admin.container.delete", user_id=auth.user.id, resource=f"container:{container_id}")
    await db.commit()


# --- Grants ---------------------------------------------------------------------------------------


@router.get("/grants")
async def list_grants(
    role_id: uuid.UUID | None = None, _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[GrantOut]:
    q = select(Grant).order_by(Grant.created_at)
    if role_id is not None:
        q = q.where(Grant.role_id == role_id)
    return [
        GrantOut(id=g.id, role_id=g.role_id, resource_type=g.resource_type, resource_id=g.resource_id, action=g.action)
        for g in (await db.scalars(q)).all()
    ]


@router.post("/grants", status_code=201)
async def create_grant(
    body: GrantIn, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> GrantOut:
    if body.action not in VALID_GRANTS[body.resource_type]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Action {body.action} is not valid on {body.resource_type}"
        )
    model = {ResourceType.container: Container, ResourceType.document: Document, ResourceType.tool: Tool}[
        body.resource_type
    ]
    if await db.get(model, body.resource_id) is None or await db.get(Role, body.role_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role or resource not found")
    g = Grant(**body.model_dump())
    db.add(g)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Grant already exists") from exc
    audit(
        db, "admin.grant.create", user_id=auth.user.id, resource=f"grant:{g.id}", details=body.model_dump(mode="json")
    )
    await db.commit()
    return GrantOut(id=g.id, **body.model_dump())


@router.delete("/grants/{grant_id}", status_code=204)
async def delete_grant(
    grant_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> None:
    g = await db.get(Grant, grant_id)
    if g is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grant not found")
    details = {
        "role_id": str(g.role_id),
        "resource_type": g.resource_type.value,
        "resource_id": str(g.resource_id),
        "action": g.action.value,
    }
    await db.delete(g)
    audit(db, "admin.grant.delete", user_id=auth.user.id, resource=f"grant:{grant_id}", details=details)
    await db.commit()


# --- Audit ----------------------------------------------------------------------------------------


@router.get("/audit")
async def list_audit(
    limit: int = Query(100, ge=1, le=1000),
    action: str | None = None,
    user_id: uuid.UUID | None = None,
    _: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AuditOut]:
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if action:
        q = q.where(AuditLog.action.startswith(action))
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    return [
        AuditOut(
            id=a.id,
            ts=a.ts,
            user_id=a.user_id,
            action=a.action,
            resource=a.resource,
            decision=a.decision,
            details=a.details,
        )
        for a in (await db.scalars(q)).all()
    ]
