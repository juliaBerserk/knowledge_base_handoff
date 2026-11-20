from enum import StrEnum

from estafeta.models import KnowledgeStatus, Role, SpaceKind


class Relation(StrEnum):
    NONE = "none"
    OWNER = "owner"
    SUPERVISOR = "supervisor"
    HANDOFF = "handoff"
    POSITION = "position"
    DEPARTMENT = "department"


def can_read(relation: Relation, kind: SpaceKind, status: KnowledgeStatus) -> bool:
    if kind is SpaceKind.PERSONAL:
        return relation in {Relation.OWNER, Relation.HANDOFF} or (
            relation is Relation.SUPERVISOR and status is KnowledgeStatus.IN_REVIEW
        )
    return (
        kind is SpaceKind.POSITION
        and relation in {Relation.POSITION, Relation.DEPARTMENT}
        and status is KnowledgeStatus.PUBLISHED
    )


def can_write(relation: Relation, status: KnowledgeStatus) -> bool:
    return relation is Relation.OWNER and status is KnowledgeStatus.DRAFT


def can_publish(
    relation: Relation, status: KnowledgeStatus, *, review_required: bool, target_allowed: bool
) -> bool:
    if not target_allowed:
        return False
    if relation is Relation.HANDOFF:
        return status in {KnowledgeStatus.DRAFT, KnowledgeStatus.IN_REVIEW}
    if relation is Relation.SUPERVISOR:
        return status is KnowledgeStatus.IN_REVIEW
    return relation is Relation.OWNER and status is KnowledgeStatus.DRAFT and not review_required


def can_copy(relation: Relation, kind: SpaceKind, status: KnowledgeStatus) -> bool:
    return kind is SpaceKind.POSITION and can_read(relation, kind, status)


def can_submit(relation: Relation, status: KnowledgeStatus, target_allowed: bool) -> bool:
    return target_allowed and can_write(relation, status)


def can_return(relation: Relation, status: KnowledgeStatus) -> bool:
    return relation is Relation.SUPERVISOR and status is KnowledgeStatus.IN_REVIEW


def can_archive(relation: Relation, status: KnowledgeStatus, *, manages_position: bool) -> bool:
    if status is KnowledgeStatus.PUBLISHED:
        return manages_position
    return can_write(relation, status)


def can_manage_handoffs(role: Role) -> bool:
    return role in {Role.HR, Role.DEPARTMENT_HEAD}
