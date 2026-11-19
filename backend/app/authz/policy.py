"""Authorization policy (allow-only).

Rules (PLAN.md section 5):
- Effective permissions = union of grants over the user's roles.
- A document is readable iff the user can read its container AND, when the document is
  `restricted`, also holds a `read` grant on the document itself. Chunks inherit from documents.
- `manage` on a container implies `contribute`, which implies `read` (ADR 0017).
- `contribute` lets a user upload documents and new versions of documents they can read.
- `manage` additionally lets a user delete documents and toggle `restricted` (lifting the flag only
  on documents the user can read). Neither changes grants or classification: those are admin-only.
- Tools need an `execute` grant.
- `is_admin` unlocks the management API only; it adds no data access (ADR 0009).
- Inactive users have no permissions at all.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.epoch import READ as READ_EPOCH
from app.authz.epoch import install as _install_epoch_listeners
from app.models import Action, Container, Document, Grant, ResourceType, User, UserRole


@dataclass(frozen=True)
class EffectivePermissions:
    user_id: uuid.UUID
    is_admin: bool = False
    role_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    readable_containers: frozenset[uuid.UUID] = field(default_factory=frozenset)
    contributable_containers: frozenset[uuid.UUID] = field(default_factory=frozenset)
    manageable_containers: frozenset[uuid.UUID] = field(default_factory=frozenset)
    readable_restricted_documents: frozenset[uuid.UUID] = field(default_factory=frozenset)
    executable_tools: frozenset[uuid.UUID] = field(default_factory=frozenset)

    @classmethod
    def none(cls, user_id: uuid.UUID) -> EffectivePermissions:
        return cls(user_id=user_id)


_CACHE: OrderedDict[tuple[uuid.UUID, int, bool, bool], EffectivePermissions] = OrderedDict()
_CACHE_SIZE = 20_000


def clear_permission_cache() -> None:
    _CACHE.clear()


async def effective_permissions(db: AsyncSession, user: User) -> EffectivePermissions:
    """A user's effective permissions, called on every request.

    Cached per process by (user, authz epoch): one indexed read of the epoch replaces the role and grant queries
    until something that affects permissions changes anywhere (app.authz.epoch). Revocation stays immediate.
    """
    if not user.is_active:
        return EffectivePermissions.none(user.id)
    epoch = await db.scalar(READ_EPOCH)
    if epoch is None:  # no state row (a freshly truncated database): never cache
        return await _compute(db, user)
    key = (user.id, int(epoch), user.is_active, user.is_admin)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    perms = await _compute(db, user)
    _CACHE[key] = perms
    if len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return perms


async def _compute(db: AsyncSession, user: User) -> EffectivePermissions:

    role_ids = frozenset((await db.scalars(select(UserRole.role_id).where(UserRole.user_id == user.id))).all())
    if not role_ids:
        return EffectivePermissions(user_id=user.id, is_admin=user.is_admin)

    rows = (
        await db.execute(
            select(Grant.resource_type, Grant.resource_id, Grant.action).where(Grant.role_id.in_(role_ids))
        )
    ).all()

    read_c: set[uuid.UUID] = set()
    contrib_c: set[uuid.UUID] = set()
    manage_c: set[uuid.UUID] = set()
    read_d: set[uuid.UUID] = set()
    exec_t: set[uuid.UUID] = set()
    for rtype, rid, action in rows:
        if rtype == ResourceType.container and action == Action.read:
            read_c.add(rid)
        elif rtype == ResourceType.container and action == Action.contribute:
            contrib_c.add(rid)
            read_c.add(rid)
        elif rtype == ResourceType.container and action == Action.manage:
            manage_c.add(rid)
            contrib_c.add(rid)
            read_c.add(rid)
        elif rtype == ResourceType.document and action == Action.read:
            read_d.add(rid)
        elif rtype == ResourceType.tool and action == Action.execute:
            exec_t.add(rid)

    return EffectivePermissions(
        user_id=user.id,
        is_admin=user.is_admin,
        role_ids=role_ids,
        readable_containers=frozenset(read_c),
        contributable_containers=frozenset(contrib_c),
        manageable_containers=frozenset(manage_c),
        readable_restricted_documents=frozenset(read_d),
        executable_tools=frozenset(exec_t),
    )


def can_read_document(
    perms: EffectivePermissions, *, container_id: uuid.UUID, document_id: uuid.UUID, restricted: bool
) -> bool:
    if container_id not in perms.readable_containers:
        return False
    return not restricted or document_id in perms.readable_restricted_documents


def can(
    perms: EffectivePermissions,
    action: Action,
    resource_type: ResourceType,
    resource_id: uuid.UUID,
    *,
    container_id: uuid.UUID | None = None,
    restricted: bool = False,
) -> bool:
    """Single decision point. Never consults the model, never infers from content."""
    if resource_type == ResourceType.container:
        if action == Action.read:
            return resource_id in perms.readable_containers
        if action == Action.contribute:
            return perms.is_admin or resource_id in perms.contributable_containers
        if action == Action.manage:
            return perms.is_admin or resource_id in perms.manageable_containers
        return False
    if resource_type == ResourceType.document:
        if container_id is None:
            return False
        if action == Action.read:
            return can_read_document(perms, container_id=container_id, document_id=resource_id, restricted=restricted)
        if action == Action.contribute:
            # New versions: managers always; contributors only for documents they can read.
            if perms.is_admin or container_id in perms.manageable_containers:
                return True
            return container_id in perms.contributable_containers and can_read_document(
                perms, container_id=container_id, document_id=resource_id, restricted=restricted
            )
        if action == Action.manage:
            return perms.is_admin or container_id in perms.manageable_containers
        return False
    if resource_type == ResourceType.tool:
        return action == Action.execute and resource_id in perms.executable_tools
    return False


def can_set_restricted(
    perms: EffectivePermissions, *, container_id: uuid.UUID, document_id: uuid.UUID, currently: bool, new: bool
) -> bool:
    """Managers may restrict any document in their container, but may only lift the flag on a
    document they can already read: otherwise un-restricting would grant them (and every reader of
    the container) access they were not given."""
    if not can(perms, Action.manage, ResourceType.document, document_id, container_id=container_id):
        return False
    if currently and not new and not perms.is_admin:
        return can_read_document(perms, container_id=container_id, document_id=document_id, restricted=True)
    return True


def content_containers(perms: EffectivePermissions) -> frozenset[uuid.UUID]:
    """Containers whose content this user may change (contribute or manage)."""
    return perms.contributable_containers | perms.manageable_containers


async def readable_document_ids(db: AsyncSession, perms: EffectivePermissions, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Re-check a set of document IDs against the current state of Postgres (defense in depth)."""
    if not ids:
        return set()
    rows = (
        await db.execute(select(Document.id, Document.container_id, Document.restricted).where(Document.id.in_(ids)))
    ).all()
    return {
        doc_id
        for doc_id, container_id, restricted in rows
        if can_read_document(perms, container_id=container_id, document_id=doc_id, restricted=restricted)
    }


async def readable_containers(db: AsyncSession, perms: EffectivePermissions) -> list[Container]:
    if not perms.readable_containers:
        return []
    return list(
        (
            await db.scalars(
                select(Container).where(Container.id.in_(perms.readable_containers)).order_by(Container.name)
            )
        ).all()
    )


_install_epoch_listeners()
