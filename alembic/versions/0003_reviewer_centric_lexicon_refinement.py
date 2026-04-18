"""Add occurrence classification and lexicon group reviews."""

from __future__ import annotations

import unicodedata

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_review_lexicon"
down_revision = "0002_mvp2_lexicon_lexemes"
branch_labels = None
depends_on = None


occurrence_script_type = postgresql.ENUM(
    "armenian",
    "latin",
    "mixed",
    "digit_mixed",
    "other",
    name="occurrence_script_type",
    create_type=False,
)
lexicon_group_review_status = postgresql.ENUM(
    "unreviewed",
    "ignored_noise",
    name="lexicon_group_review_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    occurrence_script_type.create(bind, checkfirst=True)
    lexicon_group_review_status.create(bind, checkfirst=True)

    op.add_column("occurrences", sa.Column("script_type", occurrence_script_type, nullable=True))
    op.add_column(
        "occurrences",
        sa.Column("has_digits", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "occurrences",
        sa.Column("has_latin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "occurrences",
        sa.Column("has_armenian", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("occurrences", sa.Column("token_length", sa.Integer(), nullable=True))
    op.create_index("ix_occurrences_script_type", "occurrences", ["script_type"], unique=False)

    op.create_table(
        "lexicon_group_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("normalized_form", sa.Text(), nullable=False),
        sa.Column(
            "review_status",
            lexicon_group_review_status,
            nullable=False,
            server_default="unreviewed",
        ),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_lexicon_group_reviews_user_id", "lexicon_group_reviews", ["user_id"], unique=False)
    op.create_index(
        "ix_lexicon_group_reviews_user_normalized_form",
        "lexicon_group_reviews",
        ["user_id", "normalized_form"],
        unique=True,
    )

    _backfill_occurrence_classification(bind)
    op.alter_column("occurrences", "script_type", nullable=False)


def downgrade() -> None:
    op.drop_index("ix_lexicon_group_reviews_user_normalized_form", table_name="lexicon_group_reviews")
    op.drop_index("ix_lexicon_group_reviews_user_id", table_name="lexicon_group_reviews")
    op.drop_table("lexicon_group_reviews")

    op.drop_index("ix_occurrences_script_type", table_name="occurrences")
    op.drop_column("occurrences", "token_length")
    op.drop_column("occurrences", "has_armenian")
    op.drop_column("occurrences", "has_latin")
    op.drop_column("occurrences", "has_digits")
    op.drop_column("occurrences", "script_type")

    bind = op.get_bind()
    lexicon_group_review_status.drop(bind, checkfirst=True)
    occurrence_script_type.drop(bind, checkfirst=True)


def _backfill_occurrence_classification(bind) -> None:
    select_statement = sa.text(
        """
        SELECT id, token
        FROM occurrences
        WHERE script_type IS NULL
        ORDER BY created_at ASC, id ASC
        LIMIT :limit
        """
    )
    update_statement = sa.text(
        """
        UPDATE occurrences
        SET script_type = :script_type,
            has_digits = :has_digits,
            has_latin = :has_latin,
            has_armenian = :has_armenian,
            token_length = :token_length
        WHERE id = :id
        """
    )

    while True:
        rows = bind.execute(select_statement, {"limit": 1000}).mappings().all()
        if not rows:
            break

        payload = []
        for row in rows:
            classification = _classify_token(row["token"])
            payload.append(
                {
                    "id": row["id"],
                    "script_type": classification["script_type"],
                    "has_digits": classification["has_digits"],
                    "has_latin": classification["has_latin"],
                    "has_armenian": classification["has_armenian"],
                    "token_length": classification["token_length"],
                }
            )
        bind.execute(update_statement, payload)


def _classify_token(token: str) -> dict[str, object]:
    letters = [char for char in token if unicodedata.category(char).startswith("L")]
    has_armenian = any(_is_armenian_letter(char) for char in letters)
    has_latin = any(_is_latin_letter(char) for char in letters)
    has_digits = any(char.isdigit() for char in token)

    if has_digits and letters:
        script_type = "digit_mixed"
    elif letters and all(_is_armenian_letter(char) for char in letters):
        script_type = "armenian"
    elif letters and all(_is_latin_letter(char) for char in letters):
        script_type = "latin"
    elif has_armenian and has_latin:
        script_type = "mixed"
    else:
        script_type = "other"

    return {
        "script_type": script_type,
        "has_digits": has_digits,
        "has_latin": has_latin,
        "has_armenian": has_armenian,
        "token_length": len(token),
    }


def _is_armenian_letter(char: str) -> bool:
    return unicodedata.category(char).startswith("L") and "ARMENIAN" in unicodedata.name(char, "")


def _is_latin_letter(char: str) -> bool:
    return unicodedata.category(char).startswith("L") and "LATIN" in unicodedata.name(char, "")
