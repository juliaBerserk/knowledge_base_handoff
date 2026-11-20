"""Scheduled handoffs, transfers and private retention."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_handoffs"
down_revision = "0009_rag"
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
    "knowledge_submitted",
    "knowledge_returned",
    "knowledge_archived",
    "publication_settings_changed",
    "knowledge_copied",
    "knowledge_exported",
    "ai_question",
)
NEW_ACTIONS = (
    "handoff_created",
    "handoff_cancelled",
    "handoff_activated",
    "handoff_review_closed",
    "handoff_conflict",
    "employee_transferred",
)
OLD_TARGETS = ("employee", "material", "record", "space", "chat")

ACTIVE_ASSIGNMENT = """
CREATE FUNCTION knowledge.open_assignment(member uuid)
RETURNS knowledge.position_assignments
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT a FROM knowledge.position_assignments a WHERE a.employee_id = member
        AND a.ended_at IS NULL AND a.started_at <= statement_timestamp()
$$
"""

DEPARTURE_DUE = """
CREATE FUNCTION auth.departure_due(member uuid) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT 1 FROM knowledge.handoffs h
        JOIN knowledge.position_assignments a ON a.id = h.origin_assignment_id
        WHERE h.employee_id = member AND h.kind = 'departure' AND h.status = 'scheduled'
            AND h.effective_at <= clock_timestamp() AND a.ended_at IS NULL
    )
$$
"""

ACTOR = """
CREATE FUNCTION knowledge.handoff_actor()
RETURNS TABLE (id uuid, role text, department_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT e.id, e.role::text, p.department_id FROM knowledge.employees e
    JOIN auth.credentials c ON c.employee_id = e.id
    LEFT JOIN LATERAL knowledge.open_assignment(e.id) a ON true
    LEFT JOIN knowledge.positions p ON p.id = a.position_id
    WHERE e.id = NULLIF(current_setting('app.user_id', true), '')::uuid
        AND e.role IN ('employee','department_head','hr')
        AND c.blocked_at IS NULL AND c.password_hash IS NOT NULL AND NOT auth.departure_due(e.id)
$$
"""

AUDIT_HELPER = """
CREATE FUNCTION knowledge.write_handoff_audit(event_action text, actor uuid, handoff uuid)
RETURNS void LANGUAGE sql SET search_path = pg_catalog AS $$
    INSERT INTO audit.events(action, outcome, actor_id, target_kind, target_id)
    VALUES (event_action, 'success', actor, 'handoff', handoff)
$$
"""

SCHEDULE = """
CREATE FUNCTION knowledge.schedule_handoff(
    operation text, outgoing uuid, origin uuid, successor uuid, destination uuid,
    cutoff timestamptz, retention_mode text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    actor record;
    assignment knowledge.position_assignments%ROWTYPE;
    successor_assignment uuid;
    origin_department uuid;
    destination_department uuid;
    new_id uuid := gen_random_uuid();
BEGIN
    SELECT * INTO actor FROM knowledge.handoff_actor();
    IF NOT FOUND OR actor.role NOT IN ('hr','department_head') OR outgoing = actor.id
        OR cutoff IS NULL OR cutoff <= clock_timestamp()
        OR cutoff > clock_timestamp() + interval '366 days'
        OR operation IS NULL OR operation NOT IN ('departure','transfer')
        OR retention_mode IS NULL OR retention_mode NOT IN ('archive','delete') THEN
        RETURN NULL;
    END IF;
    SELECT * INTO assignment FROM knowledge.open_assignment(outgoing);
    SELECT department_id INTO origin_department FROM knowledge.positions WHERE id = origin;
    IF assignment.id IS NULL OR assignment.position_id IS DISTINCT FROM origin
        OR origin_department IS NULL
        OR (actor.role = 'department_head' AND actor.department_id IS DISTINCT FROM
            origin_department) THEN
        RETURN NULL;
    END IF;
    IF operation = 'transfer' THEN
        SELECT department_id INTO destination_department FROM knowledge.positions
        WHERE id = destination;
        IF destination_department IS NULL OR origin = destination OR successor IS NOT NULL
            OR (actor.role = 'department_head' AND destination_department <> origin_department)
            THEN RETURN NULL;
        END IF;
    ELSIF destination IS NOT NULL OR successor IN (outgoing, actor.id) THEN
        RETURN NULL;
    END IF;
    IF successor IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM knowledge.employees e LEFT JOIN auth.credentials c ON c.employee_id = e.id
            WHERE e.id = successor AND e.role IN ('employee','department_head')
                AND c.blocked_at IS NULL AND NOT auth.departure_due(e.id)
        ) THEN RETURN NULL; END IF;
        SELECT a.id INTO successor_assignment FROM knowledge.open_assignment(successor) a;
        IF actor.role = 'department_head' AND NOT EXISTS (
            SELECT 1 FROM knowledge.position_assignments a
            JOIN knowledge.positions p ON p.id = a.position_id
            WHERE a.id = successor_assignment AND p.department_id = origin_department
        ) THEN RETURN NULL; END IF;
    END IF;
    INSERT INTO knowledge.handoffs (
        id, kind, employee_id, origin_assignment_id, position_id, department_id,
        successor_id, successor_assignment_id, target_position_id, employee_label, position_label,
        successor_label,
        created_by, effective_at, retention
    ) VALUES (
        new_id, operation, outgoing, assignment.id, origin, origin_department, successor,
        successor_assignment, destination,
        (SELECT full_name FROM knowledge.employees WHERE id = outgoing),
        (SELECT title FROM knowledge.positions WHERE id = origin),
        (SELECT full_name FROM knowledge.employees WHERE id = successor),
        actor.id, cutoff, retention_mode
    ) ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN RETURN NULL; END IF;
    PERFORM knowledge.write_handoff_audit('handoff_created', actor.id, new_id);
    RETURN new_id;
END
$$
"""

CANCEL = """
CREATE FUNCTION knowledge.cancel_handoff(target uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE actor record;
BEGIN
    SELECT * INTO actor FROM knowledge.handoff_actor();
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE knowledge.handoffs h SET status = 'cancelled'
        WHERE h.id = target AND h.status = 'scheduled' AND h.effective_at > clock_timestamp()
            AND (actor.role = 'hr'
                OR (actor.role = 'department_head' AND actor.department_id = h.department_id));
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM knowledge.write_handoff_audit('handoff_cancelled', actor.id, target);
    RETURN true;
END
$$
"""

COUNTS = """
CREATE FUNCTION knowledge.knowledge_counts(member uuid, origin uuid)
RETURNS TABLE (category text, total bigint)
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT r.category::text, count(*) FROM knowledge.records r
    JOIN knowledge.spaces s ON s.id = r.space_id
    WHERE r.author_id = member
        AND r.category IS NOT NULL AND r.cleaned_text IS NOT NULL AND r.status <> 'archived'
        AND (s.employee_id = member OR s.position_id = origin)
    GROUP BY r.category
$$
"""

OPTIONS = """
CREATE FUNCTION knowledge.handoff_options() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object(
        'employees', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', e.id, 'name', e.full_name, 'position_id', p.id,
                'position_title', p.title, 'can_succeed', e.role IN ('employee','department_head')
            ) ORDER BY e.full_name, e.id)
            FROM knowledge.employees e
            LEFT JOIN auth.credentials c ON c.employee_id = e.id
            LEFT JOIN LATERAL knowledge.open_assignment(e.id) a ON true
            LEFT JOIN knowledge.positions p ON p.id = a.position_id
            WHERE e.id <> actor.id AND c.blocked_at IS NULL AND NOT auth.departure_due(e.id)
                AND (actor.role = 'hr' OR actor.department_id = p.department_id)
        ), '[]'::jsonb),
        'positions', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', p.id, 'title', p.title, 'department', d.name
            ) ORDER BY d.name, p.title, p.id)
            FROM knowledge.positions p JOIN knowledge.departments d ON d.id = p.department_id
            WHERE actor.role = 'hr' OR actor.department_id = p.department_id
        ), '[]'::jsonb)
    )
    FROM knowledge.handoff_actor() actor WHERE actor.role IN ('hr','department_head')
$$
"""

COMPLETENESS = """
CREATE FUNCTION knowledge.handoff_completeness(target uuid)
RETURNS TABLE (category text, total bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT counts.key, counts.value::bigint
    FROM knowledge.handoffs h
    CROSS JOIN knowledge.handoff_actor() actor
    CROSS JOIN LATERAL jsonb_each_text(COALESCE(h.final_counts, (
        SELECT COALESCE(jsonb_object_agg(row.category, row.total), '{}'::jsonb)
        FROM knowledge.knowledge_counts(h.employee_id, h.position_id) row
    ))) counts
    WHERE h.id = target AND h.kind = 'departure'
        AND (
            actor.role = 'hr'
            OR (actor.role = 'department_head' AND actor.department_id = h.department_id)
            OR actor.id = h.employee_id
        )
$$
"""

PROCESS = """
CREATE FUNCTION knowledge.process_handoff() RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    item knowledge.handoffs%ROWTYPE;
    current_assignment knowledge.position_assignments%ROWTYPE;
    planned_successor knowledge.position_assignments%ROWTYPE;
    personal uuid;
    cutoff timestamptz := clock_timestamp();
    assigned boolean := false;
BEGIN
    SELECT * INTO item FROM knowledge.handoffs h
    WHERE (h.status = 'scheduled' AND h.effective_at <= cutoff)
        OR (h.status = 'review' AND h.review_until <= cutoff)
    ORDER BY CASE WHEN h.status = 'scheduled' THEN h.effective_at ELSE h.review_until END, h.id
    FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN false; END IF;

    IF item.status = 'review' THEN
        UPDATE knowledge.handoffs SET final_counts = (
            SELECT COALESCE(jsonb_object_agg(row.category, row.total), '{}'::jsonb)
            FROM knowledge.knowledge_counts(item.employee_id, item.position_id) row
        ) WHERE id = item.id;
        SELECT s.id INTO personal FROM knowledge.spaces s WHERE s.employee_id = item.employee_id
            AND s.kind = 'personal' AND s.review_until = item.review_until FOR UPDATE;
        IF personal IS NOT NULL THEN
            IF item.retention = 'delete' THEN
                UPDATE knowledge.records SET source_chunk_id = NULL WHERE source_chunk_id IN (
                    SELECT c.id FROM knowledge.chunks c JOIN knowledge.materials m
                    ON m.id = c.material_id WHERE m.space_id = personal
                );
                DELETE FROM knowledge.records WHERE space_id = personal;
                DELETE FROM knowledge.materials WHERE space_id = personal;
                DELETE FROM knowledge.spaces WHERE id = personal;
            ELSE
                UPDATE knowledge.records SET status = 'archived' WHERE space_id = personal;
                UPDATE knowledge.materials SET status = 'archived' WHERE space_id = personal;
                UPDATE knowledge.spaces SET review_until = NULL, review_department_id = NULL
                    WHERE id = personal;
            END IF;
        END IF;
        UPDATE knowledge.handoffs SET status = 'completed' WHERE id = item.id;
        PERFORM knowledge.write_handoff_audit('handoff_review_closed', NULL, item.id);
        RETURN true;
    END IF;

    -- Один порядок блокировок для параллельных эстафет и переводов.
    PERFORM e.id FROM knowledge.employees e WHERE e.id IN (item.employee_id, item.successor_id)
        ORDER BY e.id FOR NO KEY UPDATE;
    PERFORM c.employee_id FROM auth.credentials c
        WHERE c.employee_id IN (item.employee_id, item.successor_id)
        ORDER BY c.employee_id FOR UPDATE;
    SELECT * INTO current_assignment FROM knowledge.position_assignments a
        WHERE a.employee_id = item.employee_id AND a.ended_at IS NULL
            AND a.started_at <= cutoff FOR UPDATE;
    IF current_assignment.id IS DISTINCT FROM item.origin_assignment_id
        OR current_assignment.started_at >= item.effective_at
        OR (item.kind = 'transfer' AND EXISTS (
            SELECT 1 FROM auth.credentials WHERE employee_id = item.employee_id
                AND blocked_at IS NOT NULL
        )) THEN
        UPDATE knowledge.handoffs SET status = 'conflict', issue = 'assignment_changed'
            WHERE id = item.id;
        PERFORM knowledge.write_handoff_audit('handoff_conflict', NULL, item.id);
        RETURN true;
    END IF;
    UPDATE knowledge.position_assignments SET ended_at = item.effective_at
        WHERE id = current_assignment.id;
    IF item.kind = 'transfer' THEN
        INSERT INTO knowledge.position_assignments (id, employee_id, position_id, started_at)
        VALUES (gen_random_uuid(), item.employee_id, item.target_position_id, item.effective_at);
        UPDATE knowledge.handoffs SET status = 'completed' WHERE id = item.id;
        PERFORM knowledge.write_handoff_audit('employee_transferred', NULL, item.id);
        RETURN true;
    END IF;

    INSERT INTO auth.credentials (employee_id, blocked_at) VALUES (item.employee_id, cutoff)
    ON CONFLICT (employee_id) DO UPDATE SET blocked_at = COALESCE(credentials.blocked_at, cutoff);
    DELETE FROM auth.sessions WHERE employee_id = item.employee_id;
    DELETE FROM auth.invitations WHERE employee_id = item.employee_id;
    INSERT INTO knowledge.spaces(id, kind, employee_id, review_department_id, review_until)
    VALUES (gen_random_uuid(), 'personal', item.employee_id, item.department_id,
            item.effective_at + interval '720 hours')
    ON CONFLICT (employee_id) DO UPDATE SET
        review_department_id = excluded.review_department_id, review_until = excluded.review_until;

    IF item.successor_id IS NOT NULL THEN
        SELECT * INTO planned_successor FROM knowledge.position_assignments a
            WHERE a.employee_id = item.successor_id AND a.ended_at IS NULL FOR UPDATE;
        IF planned_successor.id IS NOT DISTINCT FROM item.successor_assignment_id
            AND (planned_successor.started_at IS NULL
                 OR planned_successor.started_at < item.effective_at)
            AND EXISTS (
                SELECT 1 FROM knowledge.employees e
                LEFT JOIN auth.credentials c ON c.employee_id = e.id
                WHERE e.id = item.successor_id AND e.role IN ('employee','department_head')
                    AND c.blocked_at IS NULL
            ) AND NOT EXISTS (
                SELECT 1 FROM knowledge.handoffs h WHERE h.employee_id = item.successor_id
                    AND h.status = 'scheduled'
            ) THEN
            IF planned_successor.position_id IS DISTINCT FROM item.position_id THEN
                UPDATE knowledge.position_assignments SET ended_at = item.effective_at
                    WHERE id = planned_successor.id;
                INSERT INTO knowledge.position_assignments
                    (id, employee_id, position_id, started_at)
                VALUES (gen_random_uuid(), item.successor_id, item.position_id, item.effective_at);
            END IF;
            assigned := true;
        END IF;
    END IF;
    UPDATE knowledge.handoffs SET status = 'review',
        review_until = item.effective_at + interval '720 hours', successor_assigned = assigned,
        issue = CASE WHEN item.successor_id IS NOT NULL AND NOT assigned
                     THEN 'successor_changed' END
        WHERE id = item.id;
    PERFORM knowledge.write_handoff_audit('handoff_activated', NULL, item.id);
    RETURN true;
END
$$
"""

ACTOR_PREDICATES = (
    ("knowledge.current_uploader()", "c.password_hash IS NOT NULL", "e.id"),
    ("knowledge.access_relation(uuid)", "credential.password_hash IS NOT NULL", "actor.id"),
    ("audit.can_read()", "c.password_hash IS NOT NULL", "e.id"),
)
FUNCTIONS = (
    (ACTIVE_ASSIGNMENT, "knowledge.open_assignment(uuid)", None),
    (DEPARTURE_DUE, "auth.departure_due(uuid)", "estafeta_app, estafeta_admin"),
    (ACTOR, "knowledge.handoff_actor()", "estafeta_app"),
    (AUDIT_HELPER, "knowledge.write_handoff_audit(text,uuid,uuid)", None),
    (
        SCHEDULE,
        "knowledge.schedule_handoff(text,uuid,uuid,uuid,uuid,timestamptz,text)",
        "estafeta_app",
    ),
    (CANCEL, "knowledge.cancel_handoff(uuid)", "estafeta_app"),
    (COUNTS, "knowledge.knowledge_counts(uuid,uuid)", None),
    (OPTIONS, "knowledge.handoff_options()", "estafeta_app"),
    (COMPLETENESS, "knowledge.handoff_completeness(uuid)", "estafeta_app"),
    (PROCESS, "knowledge.process_handoff()", "estafeta_worker"),
)


def patch_actor_predicates(*, install: bool) -> None:
    for signature, condition, actor in ACTOR_PREDICATES:
        guarded = f"{condition} AND NOT auth.departure_due({actor})"
        before, after = (condition, guarded) if install else (guarded, condition)
        op.execute(f"""
            DO $$
            DECLARE original text := pg_get_functiondef('{signature}'::regprocedure);
            BEGIN
                IF strpos(original, '{before}') = 0 THEN
                    RAISE EXCEPTION 'Unexpected access function definition';
                END IF;
                EXECUTE replace(original, '{before}', '{after}');
            END
            $$
        """)


def set_audit_checks(actions: tuple[str, ...], targets: tuple[str, ...]) -> None:
    for field, values in (("action", actions), ("target", targets)):
        op.drop_constraint(f"ck_event_{field}", "events", schema="audit", type_="check")
        allowed = ", ".join(f"'{value}'" for value in values)
        column = "target_kind" if field == "target" else field
        op.create_check_constraint(
            f"ck_event_{field}", "events", f"{column} IN ({allowed})", schema="audit"
        )


def upgrade() -> None:
    op.create_table(
        "handoffs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), server_default="scheduled", nullable=False),
        sa.Column(
            "employee_id", sa.Uuid(), sa.ForeignKey("knowledge.employees.id"), nullable=False
        ),
        sa.Column("employee_label", sa.String(255), nullable=False),
        sa.Column(
            "origin_assignment_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge.position_assignments.id"),
            nullable=False,
        ),
        sa.Column(
            "position_id", sa.Uuid(), sa.ForeignKey("knowledge.positions.id"), nullable=False
        ),
        sa.Column("position_label", sa.String(200), nullable=False),
        sa.Column(
            "department_id", sa.Uuid(), sa.ForeignKey("knowledge.departments.id"), nullable=False
        ),
        sa.Column("successor_id", sa.Uuid(), sa.ForeignKey("knowledge.employees.id")),
        sa.Column("successor_label", sa.String(255)),
        sa.Column(
            "successor_assignment_id", sa.Uuid(), sa.ForeignKey("knowledge.position_assignments.id")
        ),
        sa.Column("target_position_id", sa.Uuid(), sa.ForeignKey("knowledge.positions.id")),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("knowledge.employees.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_until", sa.DateTime(timezone=True)),
        sa.Column("retention", sa.String(8), server_default="archive", nullable=False),
        sa.Column("successor_assigned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("issue", sa.String(32)),
        sa.Column("final_counts", JSONB()),
        sa.CheckConstraint("kind IN ('departure','transfer')", name="ck_handoff_kind"),
        sa.CheckConstraint(
            "status IN ('scheduled','review','completed','conflict','cancelled')",
            name="ck_handoff_status",
        ),
        sa.CheckConstraint(
            "(kind = 'departure' AND target_position_id IS NULL) OR "
            "(kind = 'transfer' AND target_position_id IS NOT NULL AND successor_id IS NULL)",
            name="ck_handoff_kind_fields",
        ),
        sa.CheckConstraint("retention IN ('archive','delete')", name="ck_handoff_retention"),
        schema="knowledge",
    )
    op.create_index(
        "uq_handoff_scheduled_employee",
        "handoffs",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("status = 'scheduled'"),
        schema="knowledge",
    )
    for index, columns in (
        ("ix_handoff_schedule", ["status", "effective_at"]),
        ("ix_handoff_review", ["status", "review_until"]),
        ("ix_handoff_department", ["department_id", "effective_at"]),
        ("ix_handoff_successor", ["successor_id", "effective_at"]),
    ):
        op.create_index(index, "handoffs", columns, schema="knowledge")
    set_audit_checks((*OLD_ACTIONS, *NEW_ACTIONS), (*OLD_TARGETS, "handoff"))
    for statement, signature, role in FUNCTIONS:
        op.execute(statement)
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        if role:
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {role}")
    patch_actor_predicates(install=True)
    op.execute("ALTER TABLE knowledge.handoffs ENABLE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT ON knowledge.handoffs TO estafeta_app")
    op.execute("""
        CREATE POLICY handoff_metadata ON knowledge.handoffs FOR SELECT TO estafeta_app
        USING (EXISTS (
            SELECT 1 FROM knowledge.handoff_actor() actor
            WHERE actor.role = 'hr' OR actor.id = handoffs.employee_id
                OR (actor.role = 'department_head' AND actor.department_id = handoffs.department_id)
                OR (actor.id = handoffs.successor_id AND handoffs.successor_assigned)
        ))
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            LOCK TABLE knowledge.handoffs, audit.events IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM knowledge.handoffs) THEN
                RAISE EXCEPTION 'Downgrade would discard handoff history';
            END IF;
        END
        $$
    """)
    patch_actor_predicates(install=False)
    op.execute("DROP POLICY handoff_metadata ON knowledge.handoffs")
    for _, signature, _ in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")
    op.drop_table("handoffs", schema="knowledge")
    set_audit_checks(OLD_ACTIONS, OLD_TARGETS)
