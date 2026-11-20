from itertools import product

import pytest

from estafeta.core.permissions import (
    Relation,
    can_archive,
    can_copy,
    can_manage_handoffs,
    can_publish,
    can_read,
    can_return,
    can_submit,
    can_write,
)
from estafeta.models import KnowledgeStatus as Status
from estafeta.models import Role
from estafeta.models import SpaceKind as Space

PERSONAL_READ = {
    Relation.OWNER: set(Status),
    Relation.HANDOFF: set(Status),
    Relation.SUPERVISOR: {Status.IN_REVIEW},
}
POSITION_READ = {Relation.POSITION, Relation.DEPARTMENT}


@pytest.mark.parametrize(("relation", "kind", "status"), product(Relation, Space, Status))
def test_read_and_copy_matrix(relation: Relation, kind: Space, status: Status) -> None:
    expected_position = relation in POSITION_READ and status is Status.PUBLISHED
    expected_read = {
        Space.PERSONAL: status in PERSONAL_READ.get(relation, set()),
        Space.POSITION: expected_position,
        Space.DEPARTMENT: False,
    }[kind]
    assert can_read(relation, kind, status) == expected_read
    assert can_copy(relation, kind, status) == (kind is Space.POSITION and expected_position)


@pytest.mark.parametrize(("relation", "status"), product(Relation, Status))
def test_write_matrix(relation: Relation, status: Status) -> None:
    assert can_write(relation, status) == ((relation, status) == (Relation.OWNER, Status.DRAFT))


@pytest.mark.parametrize("role", list(Role))
def test_can_manage_handoffs(role: Role) -> None:
    assert can_manage_handoffs(role) == (role in {Role.HR, Role.DEPARTMENT_HEAD})


@pytest.mark.parametrize(
    ("relation", "status", "allowed"), product(Relation, Status, (True, False))
)
def test_workflow_permissions(relation: Relation, status: Status, allowed: bool) -> None:
    own_draft = (relation, status) == (Relation.OWNER, Status.DRAFT)
    assert can_submit(relation, status, allowed) == (allowed and own_draft)
    assert can_return(relation, status) == (
        (relation, status) == (Relation.SUPERVISOR, Status.IN_REVIEW)
    )
    assert can_archive(relation, status, manages_position=allowed) == (
        allowed if status is Status.PUBLISHED else own_draft
    )


@pytest.mark.parametrize(
    ("relation", "status", "review_required", "target_allowed"),
    product(Relation, Status, (True, False), (True, False)),
)
def test_publish_matrix(
    relation: Relation, status: Status, review_required: bool, target_allowed: bool
) -> None:
    allowed = {
        (Relation.HANDOFF, Status.DRAFT),
        (Relation.HANDOFF, Status.IN_REVIEW),
        (Relation.SUPERVISOR, Status.IN_REVIEW),
    }
    if not review_required:
        allowed.add((Relation.OWNER, Status.DRAFT))
    assert can_publish(
        relation, status, review_required=review_required, target_allowed=target_allowed
    ) == (target_allowed and (relation, status) in allowed)
