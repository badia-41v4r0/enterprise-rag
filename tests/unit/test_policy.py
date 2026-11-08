"""Permission matrix for the pure policy functions (no DB, no LLM)."""

from __future__ import annotations

import uuid

import pytest

from app.authz.policy import EffectivePermissions, can, can_read_document
from app.models import Action, Classification, ResourceType

C_HR, C_ENG, C_PUB = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
D_OPEN, D_RESTRICTED, D_OTHER_RESTRICTED = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
T_INVOICES, T_TICKETS = uuid.uuid4(), uuid.uuid4()
U = uuid.uuid4()

hr_manager = EffectivePermissions(
    user_id=U,
    readable_containers=frozenset({C_HR, C_PUB}),
    readable_restricted_documents=frozenset({D_RESTRICTED}),
    executable_tools=frozenset({T_TICKETS}),
)
engineer = EffectivePermissions(user_id=U, readable_containers=frozenset({C_ENG, C_PUB}))
admin_without_grants = EffectivePermissions(user_id=U, is_admin=True)
nobody = EffectivePermissions.none(U)


@pytest.mark.parametrize(
    ("perms", "container", "doc", "restricted", "expected"),
    [
        (hr_manager, C_HR, D_OPEN, False, True),
        (hr_manager, C_HR, D_RESTRICTED, True, True),
        (hr_manager, C_HR, D_OTHER_RESTRICTED, True, False),  # restricted needs a document grant
        (hr_manager, C_ENG, D_OPEN, False, False),
        (engineer, C_HR, D_OPEN, False, False),
        (engineer, C_ENG, D_OPEN, False, True),
        (engineer, C_PUB, D_OPEN, False, True),
        (nobody, C_PUB, D_OPEN, False, False),
        (admin_without_grants, C_HR, D_OPEN, False, False),  # ADR 0009: admin is not data access
    ],
)
def test_document_read_matrix(
    perms: EffectivePermissions, container: uuid.UUID, doc: uuid.UUID, restricted: bool, expected: bool
) -> None:
    assert can_read_document(perms, container_id=container, document_id=doc, restricted=restricted) is expected
    assert (
        can(perms, Action.read, ResourceType.document, doc, container_id=container, restricted=restricted) is expected
    )


def test_document_grant_without_container_read_is_denied() -> None:
    perms = EffectivePermissions(user_id=U, readable_restricted_documents=frozenset({D_RESTRICTED}))
    assert not can_read_document(perms, container_id=C_HR, document_id=D_RESTRICTED, restricted=True)


def test_document_without_container_is_denied() -> None:
    assert not can(hr_manager, Action.read, ResourceType.document, D_OPEN)


def test_tools_need_execute_grant() -> None:
    assert can(hr_manager, Action.execute, ResourceType.tool, T_TICKETS)
    assert not can(hr_manager, Action.execute, ResourceType.tool, T_INVOICES)
    assert not can(hr_manager, Action.read, ResourceType.tool, T_TICKETS)
    assert not can(admin_without_grants, Action.execute, ResourceType.tool, T_TICKETS)


def test_manage_is_admin_or_manage_grant() -> None:
    assert can(admin_without_grants, Action.manage, ResourceType.container, C_HR)
    assert not can(hr_manager, Action.manage, ResourceType.container, C_HR)
    manager = EffectivePermissions(
        user_id=U, manageable_containers=frozenset({C_HR}), readable_containers=frozenset({C_HR})
    )
    assert can(manager, Action.manage, ResourceType.container, C_HR)
    assert can(manager, Action.manage, ResourceType.document, D_OPEN, container_id=C_HR)


def test_classification_order() -> None:
    assert Classification.max("public", "internal") == Classification.internal
    assert Classification.max(None, "confidential", "public") == Classification.confidential
    assert Classification.max() == Classification.public
