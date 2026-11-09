"""Admin API: users, roles, containers, grants, audit. Every route requires `is_admin`."""

from __future__ import annotations

import enum
import re
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import STEP_UP_REQUIRED, AuthContext, recently_authenticated, require_admin, require_step_up
from app.authz.policy import effective_permissions
from app.core.db import get_db
from app.core.security import hash_password
from app.models import (
    Action,
    AuditLog,
    Classification,
    Container,
    Document,
    Grant,
    McpServer,
    ResourceType,
    Role,
    Session,
    Tool,
    User,
    UserRole,
)
from app.services import mfa
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
    mfa_enabled: bool = False
    must_change_password: bool = False


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


class RoleTemplate(enum.StrEnum):
    """Presets for container grants (ADR 0017)."""

    reader = "reader"
    contributor = "contributor"
    manager = "manager"


TEMPLATE_ACTION = {
    RoleTemplate.reader: Action.read,
    RoleTemplate.contributor: Action.contribute,
    RoleTemplate.manager: Action.manage,
}


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    template: RoleTemplate | None = None
    container_ids: list[uuid.UUID] = Field(default_factory=list)


class RolePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None


class RoleClone(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TemplateApply(BaseModel):
    template: RoleTemplate
    container_ids: list[uuid.UUID] = Field(min_length=1)


class Members(BaseModel):
    add: list[uuid.UUID] = Field(default_factory=list)
    remove: list[uuid.UUID] = Field(default_factory=list)


class AccessLevel(enum.StrEnum):
    none = "none"
    read = "read"
    contribute = "contribute"
    manage = "manage"


class AccessIn(BaseModel):
    level: AccessLevel


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    user_count: int = 0
    grant_count: int = 0


class EffectiveContainer(BaseModel):
    id: uuid.UUID
    name: str
    classification: Classification
    level: AccessLevel


class EffectiveDocument(BaseModel):
    id: uuid.UUID
    title: str
    container: str


class EffectiveOut(BaseModel):
    user_id: uuid.UUID
    is_admin: bool
    is_active: bool
    roles: list[str]
    containers: list[EffectiveContainer]
    restricted_documents: list[EffectiveDocument]
    tools: list[str]


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
    ResourceType.container: {Action.read, Action.contribute, Action.manage},
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
        mfa_enabled=u.mfa_enabled,
        must_change_password=u.must_change_password,
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


def _require_recent_auth(auth: AuthContext) -> None:
    if not recently_authenticated(auth.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, STEP_UP_REQUIRED)


# --- Users ----------------------------------------------------------------------------------------


@router.get("/users")
async def list_users(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[UserOut]:
    users = (await db.scalars(select(User).order_by(User.email))).all()
    return [_user_out(u) for u in users]


@router.post("/users", status_code=201)
async def create_user(
    body: UserIn, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> UserOut:
    if body.is_admin:
        _require_recent_auth(auth)
    # The password was chosen by the administrator, so it is temporary: the user replaces it at the first login
    # (PLAN-v0.4 decision 2, ADR 0037).
    user = User(
        email=body.email.strip().lower(),
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        is_admin=body.is_admin,
        must_change_password=True,
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
        # Set by an administrator for someone else: temporary, to be replaced at the next login (ADR 0037).
        user.must_change_password = user.id != auth.user.id
        changes["password"] = "changed"  # noqa: S105 - audit marker, not a secret
        if user.must_change_password:
            await db.execute(delete(Session).where(Session.user_id == user.id))
    if body.is_admin is not None:
        if body.is_admin and not user.is_admin:
            _require_recent_auth(auth)  # promoting to admin is a step-up action (ADR 0019)
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


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> None:
    """Permanently delete a deactivated user (ADR 0033).

    Deactivate first (a second, deliberate step). Their sessions, role memberships, personal quotas, MFA codes
    and conversations go with them (database cascades); rows that only record who created something keep the
    record with the creator unset; the audit log and the token usage figures (numbers only) are kept.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == auth.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete yourself")
    if user.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, "Deactivate the user before deleting them")
    details = {
        "email": user.email,
        "display_name": user.display_name,
        "was_admin": user.is_admin,
        "roles": sorted(r.name for r in user.roles),
    }
    await db.execute(delete(User).where(User.id == user_id))
    audit(db, "admin.user.delete", user_id=auth.user.id, resource=f"user:{user_id}", details=details)
    await db.commit()


@router.post("/users/{user_id}/reset-mfa", status_code=204)
async def reset_user_mfa(
    user_id: uuid.UUID, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> None:
    """Remove a user's second factor (lost device). Their sessions are ended."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    await mfa.reset(db, user)
    await db.execute(delete(Session).where(Session.user_id == user.id))
    audit(db, "admin.user.reset_mfa", user_id=auth.user.id, resource=f"user:{user.id}")
    await db.commit()


# --- Roles ----------------------------------------------------------------------------------------


async def _role_out(db: AsyncSession, r: Role) -> RoleOut:
    users = await db.scalar(select(func.count()).select_from(UserRole).where(UserRole.role_id == r.id))
    grants = await db.scalar(select(func.count()).select_from(Grant).where(Grant.role_id == r.id))
    return RoleOut(id=r.id, name=r.name, description=r.description, user_count=users or 0, grant_count=grants or 0)


async def _role_or_404(db: AsyncSession, role_id: uuid.UUID) -> Role:
    role = await db.get(Role, role_id)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")
    return role


async def _set_container_access(
    db: AsyncSession, role_id: uuid.UUID, container_id: uuid.UUID, level: AccessLevel
) -> None:
    """Replace a role's container grants on one container by a single level (or none)."""
    await db.execute(
        delete(Grant).where(
            Grant.role_id == role_id,
            Grant.resource_type == ResourceType.container,
            Grant.resource_id == container_id,
        )
    )
    if level != AccessLevel.none:
        db.add(
            Grant(
                role_id=role_id,
                resource_type=ResourceType.container,
                resource_id=container_id,
                action=Action(level.value),
            )
        )


async def _apply_template(db: AsyncSession, role: Role, template: RoleTemplate, container_ids: list[uuid.UUID]) -> None:
    found = set((await db.scalars(select(Container.id).where(Container.id.in_(container_ids)))).all())
    if found != set(container_ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown container id")
    for cid in set(container_ids):
        await _set_container_access(db, role.id, cid, AccessLevel(TEMPLATE_ACTION[template].value))


@router.get("/roles")
async def list_roles(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[RoleOut]:
    roles = (await db.scalars(select(Role).order_by(Role.name))).all()
    return [await _role_out(db, r) for r in roles]


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
    if body.template is not None and body.container_ids:
        await _apply_template(db, role, body.template, body.container_ids)
    audit(
        db,
        "admin.role.create",
        user_id=auth.user.id,
        resource=f"role:{role.id}",
        details={
            "name": role.name,
            "template": body.template.value if body.template else None,
            "container_ids": [str(c) for c in body.container_ids],
        },
    )
    await db.commit()
    return await _role_out(db, role)


@router.patch("/roles/{role_id}")
async def update_role(
    role_id: uuid.UUID, body: RolePatch, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> RoleOut:
    role = await _role_or_404(db, role_id)
    changes = body.model_dump(exclude_none=True)
    before = {"name": role.name, "description": role.description}
    for k, v in changes.items():
        setattr(role, k, v)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Role already exists") from exc
    audit(
        db, "admin.role.update", user_id=auth.user.id, resource=f"role:{role.id}", details={"before": before, **changes}
    )
    await db.commit()
    return await _role_out(db, role)


@router.post("/roles/{role_id}/clone", status_code=201)
async def clone_role(
    role_id: uuid.UUID, body: RoleClone, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> RoleOut:
    """Copy a role's grants (not its members) into a new role."""
    src = await _role_or_404(db, role_id)
    role = Role(name=body.name, description=src.description)
    db.add(role)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Role already exists") from exc
    grants = (await db.scalars(select(Grant).where(Grant.role_id == src.id))).all()
    for g in grants:
        db.add(Grant(role_id=role.id, resource_type=g.resource_type, resource_id=g.resource_id, action=g.action))
    audit(
        db,
        "admin.role.clone",
        user_id=auth.user.id,
        resource=f"role:{role.id}",
        details={"from": str(src.id), "name": role.name, "grants": len(grants)},
    )
    await db.commit()
    return await _role_out(db, role)


@router.post("/roles/{role_id}/apply-template")
async def apply_template(
    role_id: uuid.UUID,
    body: TemplateApply,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RoleOut:
    role = await _role_or_404(db, role_id)
    await _apply_template(db, role, body.template, body.container_ids)
    audit(
        db,
        "admin.role.template",
        user_id=auth.user.id,
        resource=f"role:{role.id}",
        details={"template": body.template.value, "container_ids": [str(c) for c in body.container_ids]},
    )
    await db.commit()
    return await _role_out(db, role)


@router.put("/roles/{role_id}/containers/{container_id}")
async def set_container_access(
    role_id: uuid.UUID,
    container_id: uuid.UUID,
    body: AccessIn,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RoleOut:
    """Grants-matrix cell: set a role's access level on a container (none, read, contribute, manage)."""
    role = await _role_or_404(db, role_id)
    if await db.get(Container, container_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Container not found")
    await _set_container_access(db, role.id, container_id, body.level)
    audit(
        db,
        "admin.grant.set_level",
        user_id=auth.user.id,
        resource=f"role:{role.id}",
        details={"container_id": str(container_id), "level": body.level.value},
    )
    await db.commit()
    return await _role_out(db, role)


@router.post("/roles/{role_id}/members")
async def update_members(
    role_id: uuid.UUID, body: Members, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> RoleOut:
    """Bulk assignment: add and remove users from a role in one call."""
    role = await _role_or_404(db, role_id)
    wanted = set(body.add) | set(body.remove)
    if wanted:
        found = set((await db.scalars(select(User.id).where(User.id.in_(wanted)))).all())
        if found != wanted:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown user id")
    if body.remove:
        await db.execute(delete(UserRole).where(UserRole.role_id == role.id, UserRole.user_id.in_(body.remove)))
    existing = set((await db.scalars(select(UserRole.user_id).where(UserRole.role_id == role.id))).all())
    for uid in set(body.add) - existing - set(body.remove):
        db.add(UserRole(user_id=uid, role_id=role.id))
    audit(
        db,
        "admin.role.members",
        user_id=auth.user.id,
        resource=f"role:{role.id}",
        details={"add": [str(u) for u in body.add], "remove": [str(u) for u in body.remove]},
    )
    await db.commit()
    return await _role_out(db, role)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: uuid.UUID,
    reassign_to: uuid.UUID | None = None,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a role. Refused while users hold it, unless they are moved to `reassign_to`."""
    role = await _role_or_404(db, role_id)
    members = list((await db.scalars(select(UserRole.user_id).where(UserRole.role_id == role.id))).all())
    if members and reassign_to is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Role is assigned to {len(members)} user(s); reassign them first (reassign_to=<role id>)",
        )
    if reassign_to is not None:
        if reassign_to == role.id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Cannot reassign to the role being deleted")
        await _role_or_404(db, reassign_to)
        already = set((await db.scalars(select(UserRole.user_id).where(UserRole.role_id == reassign_to))).all())
        for uid in set(members) - already:
            db.add(UserRole(user_id=uid, role_id=reassign_to))
    await db.delete(role)
    audit(
        db,
        "admin.role.delete",
        user_id=auth.user.id,
        resource=f"role:{role_id}",
        details={
            "name": role.name,
            "reassign_to": str(reassign_to) if reassign_to else None,
            "members": [str(m) for m in members],
        },
    )
    await db.commit()


@router.get("/users/{user_id}/effective-permissions")
async def user_effective_permissions(
    user_id: uuid.UUID, _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> EffectiveOut:
    """Preview what a user can do, computed by the same function the request path uses."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    await db.refresh(user, ["roles"])
    p = await effective_permissions(db, user)
    ids = p.readable_containers | p.contributable_containers | p.manageable_containers
    containers = (await db.scalars(select(Container).where(Container.id.in_(ids)).order_by(Container.name))).all()

    def level(cid: uuid.UUID) -> AccessLevel:
        if cid in p.manageable_containers:
            return AccessLevel.manage
        if cid in p.contributable_containers:
            return AccessLevel.contribute
        return AccessLevel.read

    docs = (
        await db.execute(
            select(Document.id, Document.title, Container.name)
            .join(Container, Container.id == Document.container_id)
            .where(Document.id.in_(p.readable_restricted_documents), Document.container_id.in_(p.readable_containers))
            .order_by(Document.title)
        )
    ).all()
    tools = (
        await db.execute(
            select(McpServer.name, Tool.name)
            .join(McpServer, McpServer.id == Tool.server_id)
            .where(Tool.id.in_(p.executable_tools))
            .order_by(McpServer.name, Tool.name)
        )
    ).all()
    return EffectiveOut(
        user_id=user.id,
        is_admin=user.is_admin,
        is_active=user.is_active,
        roles=sorted(r.name for r in user.roles),
        containers=[
            EffectiveContainer(id=c.id, name=c.name, classification=c.classification, level=level(c.id))
            for c in containers
        ],
        restricted_documents=[EffectiveDocument(id=i, title=t, container=cn) for i, t, cn in docs],
        tools=[f"{s}__{t}" for s, t in tools],
    )


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
