import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from estafeta.core.db import set_local_user_id
from estafeta.domain.access import (
    AccessContext,
    KnowledgeResource,
    KnowledgeScope,
    KnowledgeStatus,
    Role,
    can_write,
)
from estafeta.models import (
    Material,
    MaterialVersion,
    RedactionEvent,
    SanitizationPolicy,
    User,
)
from estafeta.schemas.audit import AuditEventType, AuditMetadata
from estafeta.schemas.materials import RedactionRange, SanitizationPolicyUpdate
from estafeta.services.audit import record_audit_event
from estafeta.services.document_input import ExtractedDocument
from estafeta.services.sanitization import (
    SANITIZER_VERSION,
    SanitizationResult,
    sanitize_text,
)


@dataclass(frozen=True, slots=True)
class PreparedMaterial:
    title: SanitizationResult
    content: SanitizationResult

    def redaction_counts(self) -> dict[str, int]:
        counts = dict(self.title.redaction_counts)
        for finding_type, count in self.content.redaction_counts.items():
            counts[finding_type] = counts.get(finding_type, 0) + count
        return dict(sorted(counts.items()))


def load_sanitization_policy(session: Session) -> SanitizationPolicyUpdate:
    row = session.get(SanitizationPolicy, 1)
    if row is None:
        return SanitizationPolicyUpdate()
    return SanitizationPolicyUpdate(
        custom_patterns=row.custom_patterns,
        custom_terms=row.custom_terms,
        corporate_domains=row.corporate_domains,
    )


def save_sanitization_policy(
    session: Session,
    *,
    updated_by: UUID,
    policy: SanitizationPolicyUpdate,
) -> SanitizationPolicy:
    set_local_user_id(session, updated_by)
    row = session.get(SanitizationPolicy, 1)
    if row is None:
        row = SanitizationPolicy(id=1, updated_by=updated_by)
        session.add(row)
    row.custom_patterns = [rule.model_dump(mode="json") for rule in policy.custom_patterns]
    row.custom_terms = policy.custom_terms
    row.corporate_domains = policy.corporate_domains
    row.updated_by = updated_by
    session.flush()
    record_audit_event(
        session,
        AuditEventType.SANITIZATION_POLICY_UPDATED,
        actor_user_id=updated_by,
        actor_role=Role.ADMIN.value,
    )
    return row


def prepare_material(
    session: Session,
    *,
    title: str,
    document: ExtractedDocument,
    approved_external_contact_ids: set[str] | None = None,
    manual_redactions: list[RedactionRange] | None = None,
) -> PreparedMaterial:
    policy = load_sanitization_policy(session)
    directory_names: set[str] = set()
    directory_emails: set[str] = set()
    for user in session.scalars(select(User).where(User.is_active.is_(True))):
        directory_names.add(user.full_name)
        directory_emails.add(user.email)

    title_result = sanitize_text(
        title,
        directory_names=directory_names,
        directory_emails=directory_emails,
        policy=policy,
        manual_redactions=manual_redactions,
    )
    content_result = sanitize_text(
        document.text,
        directory_names=directory_names,
        directory_emails=directory_emails,
        policy=policy,
        approved_external_contact_ids=approved_external_contact_ids,
    )
    if len(title_result.cleaned_text) > 300:
        raise ValueError("После очистки заголовок превышает 300 символов")
    if not title_result.cleaned_text.strip():
        raise ValueError("После очистки заголовок не должен быть пустым")
    return PreparedMaterial(title_result, content_result)


def create_material(
    session: Session,
    *,
    owner: User,
    title: str,
    document: ExtractedDocument,
    prepared: PreparedMaterial,
) -> tuple[Material, MaterialVersion, dict[str, int]]:
    set_local_user_id(session, owner.id)
    access_context = AccessContext(owner.id, owner.role, None, None, owner.is_active)
    resource = KnowledgeResource(
        owner_id=owner.id,
        scope=KnowledgeScope.PERSONAL,
        status=KnowledgeStatus.DRAFT,
    )
    if not can_write(access_context, resource):
        raise PermissionError("Недостаточно прав для создания материала")

    material = Material(
        id=uuid4(),
        owner_id=owner.id,
        title=prepared.title.cleaned_text,
        scope=KnowledgeScope.PERSONAL.value,
        status=KnowledgeStatus.DRAFT.value,
    )
    version = MaterialVersion(
        id=uuid4(),
        material_id=material.id,
        version_number=1,
        source_kind=document.source_kind,
        cleaned_text=prepared.content.cleaned_text,
        content_hash=hashlib.sha256(prepared.content.cleaned_text.encode("utf-8")).digest(),
        sanitizer_version=SANITIZER_VERSION,
        created_by=owner.id,
    )
    session.add_all((material, version))
    session.flush()

    counts = prepared.redaction_counts()
    for finding_type, redacted_count in counts.items():
        session.add(
            RedactionEvent(
                id=uuid4(),
                version_id=version.id,
                finding_type=finding_type,
                redacted_count=redacted_count,
            )
        )
        record_audit_event(
            session,
            AuditEventType.MATERIAL_REDACTION_RECORDED,
            actor_user_id=owner.id,
            actor_role=owner.role.value,
            subject_type="material",
            subject_id=material.id,
            metadata=AuditMetadata(
                finding_type=finding_type,
                redacted_count=redacted_count,
            ),
        )
    record_audit_event(
        session,
        AuditEventType.MATERIAL_CREATED,
        actor_user_id=owner.id,
        actor_role=owner.role.value,
        subject_type="material",
        subject_id=material.id,
    )
    return material, version, counts
