"""Enable RLS on lexicon group index tables."""

from __future__ import annotations

from alembic import op


revision = "0018_lexicon_index_rls"
down_revision = "0017_index_doc_script_counts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL statement_timeout = '0'")

    for table in ("lexicon_group_index", "lexicon_group_index_documents"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY lexicon_group_index_select_own
        ON lexicon_group_index
        FOR SELECT
        TO authenticated
        USING (user_id = auth.uid())
        """
    )
    op.execute(
        """
        CREATE POLICY lexicon_group_index_insert_own
        ON lexicon_group_index
        FOR INSERT
        TO authenticated
        WITH CHECK (user_id = auth.uid())
        """
    )
    op.execute(
        """
        CREATE POLICY lexicon_group_index_update_own
        ON lexicon_group_index
        FOR UPDATE
        TO authenticated
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid())
        """
    )
    op.execute(
        """
        CREATE POLICY lexicon_group_index_delete_own
        ON lexicon_group_index
        FOR DELETE
        TO authenticated
        USING (user_id = auth.uid())
        """
    )

    op.execute(
        """
        CREATE POLICY lexicon_group_index_documents_select_own
        ON lexicon_group_index_documents
        FOR SELECT
        TO authenticated
        USING (user_id = auth.uid())
        """
    )
    op.execute(
        """
        CREATE POLICY lexicon_group_index_documents_insert_own
        ON lexicon_group_index_documents
        FOR INSERT
        TO authenticated
        WITH CHECK (user_id = auth.uid())
        """
    )
    op.execute(
        """
        CREATE POLICY lexicon_group_index_documents_update_own
        ON lexicon_group_index_documents
        FOR UPDATE
        TO authenticated
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid())
        """
    )
    op.execute(
        """
        CREATE POLICY lexicon_group_index_documents_delete_own
        ON lexicon_group_index_documents
        FOR DELETE
        TO authenticated
        USING (user_id = auth.uid())
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL statement_timeout = '0'")

    for policy, table in (
        ("lexicon_group_index_select_own", "lexicon_group_index"),
        ("lexicon_group_index_insert_own", "lexicon_group_index"),
        ("lexicon_group_index_update_own", "lexicon_group_index"),
        ("lexicon_group_index_delete_own", "lexicon_group_index"),
        ("lexicon_group_index_documents_select_own", "lexicon_group_index_documents"),
        ("lexicon_group_index_documents_insert_own", "lexicon_group_index_documents"),
        ("lexicon_group_index_documents_update_own", "lexicon_group_index_documents"),
        ("lexicon_group_index_documents_delete_own", "lexicon_group_index_documents"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    for table in ("lexicon_group_index_documents", "lexicon_group_index"):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
