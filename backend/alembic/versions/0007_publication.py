"""Publication workflow and independent copies."""

import sqlalchemy as sa
from alembic import op

revision = "0007_publication"
down_revision = "0006_materials"
branch_labels = None
depends_on = None

OLD_ACTIONS = (
    "login",
    "logout",
    "password_set",
    "password_changed",
    "totp_enabled",
    "invitation_issued",
    "admin_bootstrapped",
    "knowledge_viewed",
    "material_uploaded",
    "material_redacted",
    "knowledge_published",
    "knowledge_copied",
    "knowledge_exported",
    "ai_question",
)
NEW_ACTIONS = (
    "knowledge_submitted",
    "knowledge_returned",
    "knowledge_archived",
    "publication_settings_changed",
)

CONTEXT = """
CREATE FUNCTION knowledge.publication_context(space uuid)
RETURNS TABLE (relation text, destination uuid, manages_position boolean, review_required boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT access.relation, destination.position_id,
           COALESCE(actor.role = 'department_head' AND acting.department_id = p.department_id,
                    false),
           cfg.review_required
    FROM knowledge.spaces s
    JOIN knowledge.employees actor ON actor.id = knowledge.current_uploader()
    CROSS JOIN LATERAL (SELECT knowledge.access_relation(s.id) AS relation) access
    LEFT JOIN knowledge.positions p ON p.id = s.position_id
    LEFT JOIN LATERAL (
        SELECT pos.department_id
        FROM knowledge.position_assignments a
        JOIN knowledge.positions pos ON pos.id = a.position_id
        WHERE a.employee_id = actor.id AND a.started_at <= statement_timestamp()
            AND (a.ended_at IS NULL OR a.ended_at > statement_timestamp())
        ORDER BY a.started_at DESC, a.id LIMIT 1
    ) acting ON true
    LEFT JOIN LATERAL (
        SELECT a.position_id
        FROM knowledge.position_assignments a
        JOIN knowledge.positions pos ON pos.id = a.position_id
        WHERE a.employee_id = s.employee_id AND a.started_at <= statement_timestamp()
            AND (CASE WHEN access.relation = 'handoff'
                      THEN pos.department_id = s.review_department_id
                      ELSE a.ended_at IS NULL OR a.ended_at > statement_timestamp() END)
        ORDER BY a.started_at DESC, a.id LIMIT 1
    ) destination ON access.relation IN ('owner', 'supervisor', 'handoff')
    CROSS JOIN knowledge.publication_settings cfg
    WHERE s.id = space AND access.relation <> 'none'
$$
"""

TRANSITION = """
CREATE FUNCTION knowledge.transition_item(item_table text, item_id uuid, operation text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    item record;
    ctx record;
    next_status text;
    next_space uuid;
    next_kind text;
BEGIN
    IF knowledge.current_uploader() IS NULL OR item_table IS NULL
        OR item_table NOT IN ('materials', 'records') THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM knowledge.publication_settings WHERE id = 1 FOR SHARE;
    EXECUTE format('SELECT space_id, space_kind, status FROM knowledge.%I WHERE id = $1 FOR UPDATE',
                   item_table) INTO item USING item_id;
    IF item.space_id IS NULL THEN RETURN false; END IF;
    SELECT * INTO ctx FROM knowledge.publication_context(item.space_id);
    IF NOT FOUND THEN RETURN false; END IF;

    next_space := item.space_id;
    next_kind := item.space_kind;
    IF operation = 'submit' AND ctx.relation = 'owner' AND item.status = 'draft'
        AND ctx.destination IS NOT NULL THEN
        next_status := 'in_review';
    ELSIF operation = 'return' AND ctx.relation = 'supervisor' AND item.status = 'in_review' THEN
        next_status := 'draft';
    ELSIF operation = 'archive' AND (
        (ctx.relation = 'owner' AND item.status = 'draft')
        OR (ctx.manages_position AND item.status = 'published')
    ) THEN
        next_status := 'archived';
    ELSIF operation = 'publish' AND ctx.destination IS NOT NULL AND (
        (ctx.relation = 'owner' AND item.status = 'draft' AND NOT ctx.review_required)
        OR (ctx.relation = 'supervisor' AND item.status = 'in_review')
        OR (ctx.relation = 'handoff' AND item.status IN ('draft', 'in_review'))
    ) THEN
        INSERT INTO knowledge.spaces(id, kind, position_id)
        VALUES (gen_random_uuid(), 'position', ctx.destination)
        ON CONFLICT (position_id) DO NOTHING;
        SELECT id INTO next_space FROM knowledge.spaces WHERE position_id = ctx.destination;
        next_kind := 'position';
        next_status := 'published';
    ELSE
        RETURN false;
    END IF;
    EXECUTE format('UPDATE knowledge.%I SET status = $2, space_id = $3, space_kind = $4 '
                   'WHERE id = $1', item_table)
        USING item_id, next_status, next_space, next_kind;
    INSERT INTO audit.events (action, outcome, actor_id, target_kind, target_id)
    VALUES (
        CASE operation WHEN 'submit' THEN 'knowledge_submitted'
                       WHEN 'return' THEN 'knowledge_returned'
                       WHEN 'archive' THEN 'knowledge_archived'
                       WHEN 'publish' THEN 'knowledge_published' END,
        'success', knowledge.current_uploader(),
        CASE item_table WHEN 'records' THEN 'record' ELSE 'material' END, item_id
    );
    RETURN true;
END
$$
"""

COPY = """
CREATE FUNCTION knowledge.copy_record(source_id uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    actor uuid := knowledge.current_uploader();
    source knowledge.records%ROWTYPE;
    personal_space uuid;
    copy_id uuid := gen_random_uuid();
BEGIN
    IF actor IS NULL THEN RETURN NULL; END IF;
    SELECT * INTO source FROM knowledge.records WHERE id = source_id FOR SHARE;
    IF NOT FOUND OR source.space_kind <> 'position' OR source.status <> 'published'
        OR knowledge.access_relation(source.space_id) NOT IN ('position', 'department') THEN
        RETURN NULL;
    END IF;
    INSERT INTO knowledge.spaces (id, kind, employee_id)
    VALUES (gen_random_uuid(), 'personal', actor) ON CONFLICT (employee_id) DO NOTHING;
    SELECT id INTO personal_space FROM knowledge.spaces WHERE employee_id = actor;
    INSERT INTO knowledge.records
        (id, space_id, space_kind, author_id, status, cleaned_text,
         original_id, original_author_id)
    VALUES (copy_id, personal_space, 'personal', actor, 'draft', source.cleaned_text,
            source.id, source.author_id);
    INSERT INTO audit.events (action, outcome, actor_id, target_kind, target_id)
    VALUES ('knowledge_copied', 'success', actor, 'record', source.id);
    RETURN copy_id;
END
$$
"""


def set_audit_actions(actions: tuple[str, ...]) -> None:
    op.drop_constraint("ck_event_action", "events", schema="audit", type_="check")
    op.alter_column("events", "action", type_=sa.String(max(map(len, actions))), schema="audit")
    values = ", ".join(f"'{action}'" for action in actions)
    op.create_check_constraint("ck_event_action", "events", f"action IN ({values})", schema="audit")


def upgrade() -> None:
    op.create_table(
        "publication_settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("review_required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_publication_singleton"),
        schema="knowledge",
    )
    op.execute("INSERT INTO knowledge.publication_settings (id) VALUES (1)")
    op.execute("GRANT SELECT ON knowledge.publication_settings TO estafeta_app, estafeta_admin")
    op.execute("GRANT UPDATE (review_required) ON knowledge.publication_settings TO estafeta_admin")
    op.add_column("records", sa.Column("cleaned_text", sa.Text()), schema="knowledge")
    for column, table in (("original_id", "records"), ("original_author_id", "employees")):
        op.add_column("records", sa.Column(column, sa.Uuid()), schema="knowledge")
        op.create_foreign_key(
            f"fk_record_{column}",
            "records",
            table,
            [column],
            ["id"],
            source_schema="knowledge",
            referent_schema="knowledge",
        )
    op.create_check_constraint(
        "ck_record_origin_pair",
        "records",
        "(original_id IS NULL) = (original_author_id IS NULL)",
        schema="knowledge",
    )
    op.create_index(
        "ix_knowledge_records_original_id", "records", ["original_id"], schema="knowledge"
    )
    for statement, signature in (
        (CONTEXT, "publication_context(uuid)"),
        (TRANSITION, "transition_item(text,uuid,text)"),
        (COPY, "copy_record(uuid)"),
    ):
        op.execute(statement)
        op.execute(f"REVOKE ALL ON FUNCTION knowledge.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION knowledge.{signature} TO estafeta_app")
    set_audit_actions((*OLD_ACTIONS, *NEW_ACTIONS))


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            LOCK TABLE knowledge.records, audit.events IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM knowledge.records
                       WHERE cleaned_text IS NOT NULL OR original_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Downgrade would discard record content or provenance';
            END IF;
        END
        $$
    """)
    set_audit_actions(OLD_ACTIONS)
    op.execute("DROP FUNCTION knowledge.copy_record(uuid)")
    op.execute("DROP FUNCTION knowledge.transition_item(text,uuid,text)")
    op.execute("DROP FUNCTION knowledge.publication_context(uuid)")
    op.drop_constraint("ck_record_origin_pair", "records", schema="knowledge")
    for column in ("original_author_id", "original_id"):
        op.drop_constraint(f"fk_record_{column}", "records", schema="knowledge")
        op.drop_column("records", column, schema="knowledge")
    op.drop_column("records", "cleaned_text", schema="knowledge")
    op.drop_table("publication_settings", schema="knowledge")
