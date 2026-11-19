from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    EMPLOYEE = "employee"
    DEPARTMENT_MANAGER = "department_manager"
    HR = "hr"
    ADMIN = "admin"
    SECURITY_AUDITOR = "security_auditor"


class KnowledgeScope(StrEnum):
    PERSONAL = "personal"
    POSITION = "position"
    DEPARTMENT = "department"


class KnowledgeStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class KnowledgeCategory(StrEnum):
    RESPONSIBILITIES = "responsibilities"
    PROCESSES = "processes"
    CASES = "cases"
    PEOPLE = "people"
    SYSTEMS = "systems"
    DECISIONS = "decisions"
    UNFINISHED = "unfinished"
    FAQS = "faqs"
    GLOSSARY = "glossary"
    RISKS = "risks"


@dataclass(frozen=True, slots=True)
class AccessContext:
    user_id: UUID
    role: Role
    department_id: UUID | None
    position_id: UUID | None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class KnowledgeResource:
    owner_id: UUID
    scope: KnowledgeScope
    status: KnowledgeStatus
    department_id: UUID | None = None
    position_id: UUID | None = None


def _is_owner(context: AccessContext, resource: KnowledgeResource) -> bool:
    return context.user_id == resource.owner_id


def _same_department(context: AccessContext, resource: KnowledgeResource) -> bool:
    return (
        context.department_id is not None
        and resource.department_id is not None
        and context.department_id == resource.department_id
    )


def _same_position(context: AccessContext, resource: KnowledgeResource) -> bool:
    return (
        context.position_id is not None
        and resource.position_id is not None
        and context.position_id == resource.position_id
    )


def _can_access_knowledge(context: AccessContext) -> bool:
    return context.is_active and context.role not in {Role.ADMIN, Role.SECURITY_AUDITOR}


def can_read(context: AccessContext, resource: KnowledgeResource) -> bool:
    if not _can_access_knowledge(context):
        return False
    if resource.scope is KnowledgeScope.PERSONAL:
        return _is_owner(context, resource)
    if resource.status is KnowledgeStatus.ARCHIVED:
        return _is_owner(context, resource) and _same_position(context, resource)
    if resource.scope is KnowledgeScope.DEPARTMENT:
        return resource.status is KnowledgeStatus.PUBLISHED and _same_department(context, resource)
    if _is_owner(context, resource) and _same_position(context, resource):
        return True
    if resource.status is KnowledgeStatus.PUBLISHED:
        return _same_position(context, resource) or _same_department(context, resource)
    return (
        resource.status is KnowledgeStatus.IN_REVIEW
        and context.role is Role.DEPARTMENT_MANAGER
        and _same_department(context, resource)
    )


def can_write(context: AccessContext, resource: KnowledgeResource) -> bool:
    return (
        _can_access_knowledge(context)
        and resource.scope is not KnowledgeScope.DEPARTMENT
        and _is_owner(context, resource)
        and resource.status is KnowledgeStatus.DRAFT
        and (resource.scope is KnowledgeScope.PERSONAL or _same_position(context, resource))
    )


def can_submit_for_review(
    context: AccessContext,
    resource: KnowledgeResource,
    review_required: bool,
) -> bool:
    return (
        _can_access_knowledge(context)
        and review_required
        and resource.scope is KnowledgeScope.POSITION
        and resource.status is KnowledgeStatus.DRAFT
        and _is_owner(context, resource)
        and _same_position(context, resource)
    )


def can_publish(
    context: AccessContext,
    resource: KnowledgeResource,
    review_required: bool,
) -> bool:
    if not _can_access_knowledge(context) or resource.scope is not KnowledgeScope.POSITION:
        return False
    if resource.status is KnowledgeStatus.DRAFT:
        return (
            not review_required
            and _is_owner(context, resource)
            and _same_position(context, resource)
        )
    return (
        review_required
        and resource.status is KnowledgeStatus.IN_REVIEW
        and context.role is Role.DEPARTMENT_MANAGER
        and _same_department(context, resource)
    )


def can_archive(context: AccessContext, resource: KnowledgeResource) -> bool:
    if (
        not _can_access_knowledge(context)
        or resource.scope is not KnowledgeScope.POSITION
        or resource.status is not KnowledgeStatus.PUBLISHED
    ):
        return False
    return (_is_owner(context, resource) and _same_position(context, resource)) or (
        context.role is Role.DEPARTMENT_MANAGER and _same_department(context, resource)
    )
