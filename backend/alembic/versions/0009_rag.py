"""Personal chat history and embedding compatibility."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_rag"
down_revision = "0008_indexing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("embedding_key", sa.String(64)), schema="knowledge")
    op.execute("""
        CREATE FUNCTION knowledge.text_sha256(source text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path = pg_catalog AS $$
            SELECT encode(sha256(convert_to(source, 'UTF8')), 'hex')
        $$
    """)
    op.add_column(
        "materials",
        sa.Column(
            "indexed_text_hash",
            sa.String(64),
            sa.Computed("knowledge.text_sha256(cleaned_text)", persisted=True),
        ),
        schema="knowledge",
    )
    op.execute("""
        ALTER POLICY visible_chunks ON knowledge.chunks USING (active AND EXISTS (
            SELECT 1 FROM knowledge.materials m
            WHERE m.id = material_id AND m.status <> 'archived'
                AND source_hash = m.indexed_text_hash
        ))
    """)
    op.create_table(
        "chats",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge.employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="knowledge",
    )
    op.create_table(
        "chat_turns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "chat_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge.chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("question", sa.String(1000), nullable=False),
        sa.Column("scope", sa.String(12), nullable=False),
        sa.Column("segments", JSONB(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("rating", sa.SmallInteger()),
        sa.Column("comment", sa.String(1000)),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_chat_turn_rating"),
        sa.CheckConstraint("scope IN ('personal','position','department')", name="ck_chat_scope"),
        schema="knowledge",
    )
    op.create_index(
        "ix_chats_owner_created", "chats", ["owner_id", "created_at", "id"], schema="knowledge"
    )
    op.create_index(
        "ix_chat_turns_chat_created",
        "chat_turns",
        ["chat_id", "created_at", "id"],
        schema="knowledge",
    )
    op.execute("ALTER TABLE knowledge.chats ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE knowledge.chat_turns ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY own_chat ON knowledge.chats TO estafeta_app
        USING (owner_id = (SELECT knowledge.current_uploader()))
        WITH CHECK (owner_id = (SELECT knowledge.current_uploader()))
    """)
    op.execute("""
        CREATE POLICY own_turn ON knowledge.chat_turns TO estafeta_app
        USING (EXISTS (SELECT 1 FROM knowledge.chats chat WHERE chat.id = chat_id))
        WITH CHECK (EXISTS (SELECT 1 FROM knowledge.chats chat WHERE chat.id = chat_id))
    """)
    op.execute("GRANT SELECT, DELETE, INSERT (id, owner_id) ON knowledge.chats TO estafeta_app")
    op.execute("""
        GRANT SELECT,
              INSERT (id, chat_id, question, scope, segments, model_version, prompt_version),
              UPDATE (rating, comment) ON knowledge.chat_turns TO estafeta_app
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            LOCK TABLE knowledge.chats, knowledge.chat_turns IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM knowledge.chats)
            THEN RAISE EXCEPTION 'Downgrade would discard chat history';
            END IF;
        END
        $$
    """)
    op.drop_table("chat_turns", schema="knowledge")
    op.drop_table("chats", schema="knowledge")
    op.execute("""
        ALTER POLICY visible_chunks ON knowledge.chunks USING (active AND EXISTS (
            SELECT 1 FROM knowledge.materials m
            WHERE m.id = material_id AND m.status <> 'archived'
                AND source_hash = encode(sha256(convert_to(m.cleaned_text, 'UTF8')), 'hex')
        ))
    """)
    op.drop_column("materials", "indexed_text_hash", schema="knowledge")
    op.execute("DROP FUNCTION knowledge.text_sha256(text)")
    op.drop_column("chunks", "embedding_key", schema="knowledge")
