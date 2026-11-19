from uuid import UUID

import pytest

from estafeta.domain.access import (
    AccessContext,
    KnowledgeResource,
    KnowledgeScope,
    KnowledgeStatus,
    Role,
    can_archive,
    can_publish,
    can_read,
    can_submit_for_review,
    can_write,
)

USER = UUID("00000000-0000-0000-0000-000000000001")
OTHER_USER = UUID("00000000-0000-0000-0000-000000000002")
DEPARTMENT = UUID("00000000-0000-0000-0000-000000000010")
OTHER_DEPARTMENT = UUID("00000000-0000-0000-0000-000000000011")
POSITION = UUID("00000000-0000-0000-0000-000000000020")
OTHER_POSITION = UUID("00000000-0000-0000-0000-000000000021")


def context(
    *,
    user_id: UUID = USER,
    role: Role = Role.EMPLOYEE,
    department_id: UUID | None = DEPARTMENT,
    position_id: UUID | None = POSITION,
    is_active: bool = True,
) -> AccessContext:
    return AccessContext(user_id, role, department_id, position_id, is_active)


def resource(
    *,
    owner_id: UUID = OTHER_USER,
    scope: KnowledgeScope = KnowledgeScope.POSITION,
    status: KnowledgeStatus = KnowledgeStatus.DRAFT,
    department_id: UUID | None = DEPARTMENT,
    position_id: UUID | None = POSITION,
) -> KnowledgeResource:
    return KnowledgeResource(owner_id, scope, status, department_id, position_id)


@pytest.mark.parametrize(
    ("actor", "item", "expected"),
    [
        (context(is_active=False), resource(owner_id=USER), False),
        (
            context(),
            resource(owner_id=USER, scope=KnowledgeScope.PERSONAL),
            True,
        ),
        (
            context(),
            resource(scope=KnowledgeScope.PERSONAL),
            False,
        ),
        (
            context(),
            resource(owner_id=USER, scope=KnowledgeScope.DEPARTMENT),
            False,
        ),
        (
            context(),
            resource(scope=KnowledgeScope.DEPARTMENT, status=KnowledgeStatus.PUBLISHED),
            True,
        ),
        (
            context(department_id=None),
            resource(scope=KnowledgeScope.DEPARTMENT, status=KnowledgeStatus.PUBLISHED),
            False,
        ),
        (
            context(),
            resource(
                scope=KnowledgeScope.DEPARTMENT,
                status=KnowledgeStatus.PUBLISHED,
                department_id=OTHER_DEPARTMENT,
            ),
            False,
        ),
        (
            context(),
            resource(scope=KnowledgeScope.DEPARTMENT, status=KnowledgeStatus.DRAFT),
            False,
        ),
        (
            context(),
            resource(owner_id=USER, status=KnowledgeStatus.ARCHIVED),
            True,
        ),
        (
            context(),
            resource(status=KnowledgeStatus.ARCHIVED),
            False,
        ),
        (
            context(),
            resource(owner_id=USER),
            True,
        ),
        (
            context(position_id=OTHER_POSITION),
            resource(owner_id=USER),
            False,
        ),
        (
            context(position_id=None),
            resource(status=KnowledgeStatus.PUBLISHED, department_id=None),
            False,
        ),
        (
            context(position_id=None),
            resource(
                status=KnowledgeStatus.PUBLISHED,
                department_id=None,
                position_id=None,
            ),
            False,
        ),
        (
            context(position_id=OTHER_POSITION),
            resource(status=KnowledgeStatus.PUBLISHED, department_id=None),
            False,
        ),
        (
            context(position_id=None),
            resource(status=KnowledgeStatus.PUBLISHED),
            True,
        ),
        (
            context(department_id=None),
            resource(status=KnowledgeStatus.PUBLISHED, department_id=None),
            True,
        ),
        (
            context(),
            resource(
                status=KnowledgeStatus.PUBLISHED,
                department_id=OTHER_DEPARTMENT,
                position_id=OTHER_POSITION,
            ),
            False,
        ),
        (
            context(role=Role.DEPARTMENT_MANAGER),
            resource(status=KnowledgeStatus.IN_REVIEW),
            True,
        ),
        (
            context(role=Role.EMPLOYEE),
            resource(status=KnowledgeStatus.IN_REVIEW),
            False,
        ),
        (
            context(role=Role.DEPARTMENT_MANAGER),
            resource(status=KnowledgeStatus.IN_REVIEW, department_id=OTHER_DEPARTMENT),
            False,
        ),
        (context(), resource(), False),
    ],
)
def test_can_read(actor: AccessContext, item: KnowledgeResource, expected: bool) -> None:
    assert can_read(actor, item) is expected


@pytest.mark.parametrize(
    ("actor", "item", "expected"),
    [
        (context(is_active=False, user_id=USER), resource(owner_id=USER), False),
        (
            context(),
            resource(owner_id=USER, scope=KnowledgeScope.DEPARTMENT),
            False,
        ),
        (context(), resource(owner_id=OTHER_USER), False),
        (
            context(),
            resource(owner_id=USER, status=KnowledgeStatus.IN_REVIEW),
            False,
        ),
        (
            context(position_id=OTHER_POSITION),
            resource(owner_id=USER),
            False,
        ),
        (context(), resource(owner_id=USER), True),
    ],
)
def test_can_write(actor: AccessContext, item: KnowledgeResource, expected: bool) -> None:
    assert can_write(actor, item) is expected


@pytest.mark.parametrize(
    ("actor", "item", "review_required", "expected"),
    [
        (context(is_active=False, user_id=USER), resource(owner_id=USER), True, False),
        (context(user_id=USER), resource(owner_id=USER), False, False),
        (
            context(user_id=USER),
            resource(owner_id=USER, scope=KnowledgeScope.PERSONAL),
            True,
            False,
        ),
        (
            context(user_id=USER),
            resource(owner_id=USER, status=KnowledgeStatus.IN_REVIEW),
            True,
            False,
        ),
        (context(), resource(owner_id=OTHER_USER), True, False),
        (
            context(position_id=OTHER_POSITION),
            resource(owner_id=USER),
            True,
            False,
        ),
        (context(user_id=USER), resource(owner_id=USER), True, True),
    ],
)
def test_can_submit_for_review(
    actor: AccessContext,
    item: KnowledgeResource,
    review_required: bool,
    expected: bool,
) -> None:
    assert can_submit_for_review(actor, item, review_required) is expected


@pytest.mark.parametrize(
    ("actor", "item", "review_required", "expected"),
    [
        (context(is_active=False), resource(), True, False),
        (
            context(),
            resource(scope=KnowledgeScope.PERSONAL),
            True,
            False,
        ),
        (
            context(user_id=USER),
            resource(owner_id=USER),
            False,
            True,
        ),
        (
            context(user_id=USER),
            resource(owner_id=USER),
            True,
            False,
        ),
        (
            context(user_id=USER, position_id=OTHER_POSITION),
            resource(owner_id=USER),
            False,
            False,
        ),
        (
            context(),
            resource(),
            False,
            False,
        ),
        (
            context(role=Role.DEPARTMENT_MANAGER),
            resource(status=KnowledgeStatus.IN_REVIEW),
            True,
            True,
        ),
        (
            context(role=Role.EMPLOYEE),
            resource(status=KnowledgeStatus.IN_REVIEW),
            True,
            False,
        ),
        (
            context(role=Role.DEPARTMENT_MANAGER),
            resource(status=KnowledgeStatus.IN_REVIEW),
            False,
            False,
        ),
        (
            context(role=Role.DEPARTMENT_MANAGER),
            resource(status=KnowledgeStatus.DRAFT),
            True,
            False,
        ),
        (
            context(role=Role.DEPARTMENT_MANAGER),
            resource(status=KnowledgeStatus.IN_REVIEW, department_id=None),
            True,
            False,
        ),
    ],
)
def test_can_publish(
    actor: AccessContext,
    item: KnowledgeResource,
    review_required: bool,
    expected: bool,
) -> None:
    assert can_publish(actor, item, review_required) is expected


@pytest.mark.parametrize(
    ("actor", "item", "expected"),
    [
        (context(is_active=False), resource(status=KnowledgeStatus.PUBLISHED), False),
        (
            context(role=Role.ADMIN),
            resource(status=KnowledgeStatus.PUBLISHED),
            False,
        ),
        (
            context(),
            resource(scope=KnowledgeScope.PERSONAL, status=KnowledgeStatus.PUBLISHED),
            False,
        ),
        (context(), resource(status=KnowledgeStatus.DRAFT), False),
        (
            context(user_id=USER),
            resource(owner_id=USER, status=KnowledgeStatus.PUBLISHED),
            True,
        ),
        (
            context(user_id=USER, position_id=OTHER_POSITION),
            resource(owner_id=USER, status=KnowledgeStatus.PUBLISHED),
            False,
        ),
        (
            context(role=Role.DEPARTMENT_MANAGER),
            resource(status=KnowledgeStatus.PUBLISHED),
            True,
        ),
        (
            context(role=Role.DEPARTMENT_MANAGER),
            resource(status=KnowledgeStatus.PUBLISHED, department_id=OTHER_DEPARTMENT),
            False,
        ),
        (
            context(role=Role.EMPLOYEE),
            resource(status=KnowledgeStatus.PUBLISHED),
            False,
        ),
        (
            context(role=Role.DEPARTMENT_MANAGER, department_id=None),
            resource(status=KnowledgeStatus.PUBLISHED, department_id=None),
            False,
        ),
    ],
)
def test_can_archive(actor: AccessContext, item: KnowledgeResource, expected: bool) -> None:
    assert can_archive(actor, item) is expected


@pytest.mark.parametrize("role", [Role.ADMIN, Role.SECURITY_AUDITOR])
def test_administrators_and_auditors_cannot_access_knowledge_content(role: Role) -> None:
    actor = context(role=role)
    personal_draft = resource(
        owner_id=USER,
        scope=KnowledgeScope.PERSONAL,
        status=KnowledgeStatus.DRAFT,
    )
    position_draft = resource(owner_id=USER)

    assert can_read(actor, personal_draft) is False
    assert can_write(actor, personal_draft) is False
    assert can_submit_for_review(actor, position_draft, review_required=True) is False
    assert can_publish(actor, position_draft, review_required=False) is False
