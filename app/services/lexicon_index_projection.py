from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import OccurrenceScriptType


def sql_rebuild_available(session: Session) -> bool:
    bind = session.get_bind()
    if bind is None:
        return False
    return bind.dialect.name == "postgresql"


SAMPLE_LIMIT = 5

REBUILD_DOCUMENT_SLICES_SQL = text(
    """
    INSERT INTO lexicon_group_index_documents (
        user_id,
        normalized_form,
        document_id,
        occurrence_count,
        page_count,
        sample_tokens,
        sample_contexts,
        page_ids,
        script_counts,
        updated_at
    )
    SELECT
        :user_id,
        grouped.normalized_token,
        :document_id,
        grouped.occurrence_count,
        grouped.page_count,
        COALESCE(token_samples.sample_tokens, '[]'::jsonb),
        COALESCE(context_samples.sample_contexts, '[]'::jsonb),
        COALESCE(grouped.page_ids, '[]'::jsonb),
        COALESCE(grouped.script_counts, '{}'::jsonb),
        NOW()
    FROM (
        SELECT
            o.normalized_token,
            COUNT(*)::int AS occurrence_count,
            COUNT(DISTINCT o.page_id)::int AS page_count,
            jsonb_agg(DISTINCT o.page_id::text ORDER BY o.page_id::text) AS page_ids,
            (
                SELECT jsonb_object_agg(script_agg.script_key, script_agg.script_count)
                FROM (
                    SELECT
                        o2.script_type::text AS script_key,
                        COUNT(*)::int AS script_count
                    FROM occurrences o2
                    WHERE o2.document_id = :document_id
                      AND o2.normalized_token = o.normalized_token
                    GROUP BY o2.script_type
                ) AS script_agg
            ) AS script_counts
        FROM occurrences o
        WHERE o.document_id = :document_id
        GROUP BY o.normalized_token
    ) AS grouped
    LEFT JOIN LATERAL (
        SELECT COALESCE(jsonb_agg(sample_rows.token), '[]'::jsonb) AS sample_tokens
        FROM (
            SELECT DISTINCT ON (o3.token) o3.token
            FROM occurrences o3
            WHERE o3.document_id = :document_id
              AND o3.normalized_token = grouped.normalized_token
            ORDER BY o3.token, o3.page_number, o3.char_start NULLS FIRST
            LIMIT :sample_limit
        ) AS sample_rows
    ) AS token_samples ON TRUE
    LEFT JOIN LATERAL (
        SELECT COALESCE(jsonb_agg(sample_rows.context_snippet), '[]'::jsonb) AS sample_contexts
        FROM (
            SELECT DISTINCT ON (o3.context_snippet) o3.context_snippet
            FROM occurrences o3
            WHERE o3.document_id = :document_id
              AND o3.normalized_token = grouped.normalized_token
            ORDER BY o3.context_snippet, o3.page_number, o3.char_start NULLS FIRST
            LIMIT :sample_limit
        ) AS sample_rows
    ) AS context_samples ON TRUE
    ON CONFLICT (user_id, normalized_form, document_id) DO UPDATE SET
        occurrence_count = EXCLUDED.occurrence_count,
        page_count = EXCLUDED.page_count,
        sample_tokens = EXCLUDED.sample_tokens,
        sample_contexts = EXCLUDED.sample_contexts,
        page_ids = EXCLUDED.page_ids,
        script_counts = EXCLUDED.script_counts,
        updated_at = EXCLUDED.updated_at
    RETURNING normalized_form
    """
)

REBUILD_GLOBAL_ROWS_SQL = text(
    """
    INSERT INTO lexicon_group_index (
        user_id,
        normalized_form,
        occurrence_count,
        document_count,
        page_count,
        dominant_script_type,
        sample_tokens,
        sample_contexts,
        sample_document_titles,
        script_counts,
        updated_at
    )
    SELECT
        slices.user_id,
        slices.normalized_form,
        slices.occurrence_count,
        slices.document_count,
        COALESCE(page_totals.page_count, 0),
        CASE
            WHEN slices.dominant_script_key IS NULL THEN 'other'
            ELSE slices.dominant_script_key
        END::occurrence_script_type,
        COALESCE(sample_merge.sample_tokens, '[]'::jsonb),
        COALESCE(sample_merge.sample_contexts, '[]'::jsonb),
        COALESCE(sample_merge.sample_document_titles, '[]'::jsonb),
        COALESCE(slices.script_counts, '{}'::jsonb),
        NOW()
    FROM (
        SELECT
            d.user_id,
            d.normalized_form,
            SUM(d.occurrence_count)::int AS occurrence_count,
            COUNT(*)::int AS document_count,
            (
                SELECT script_key
                FROM (
                    SELECT
                        key AS script_key,
                        SUM(value::int) AS script_total
                    FROM lexicon_group_index_documents d2
                    CROSS JOIN LATERAL jsonb_each_text(COALESCE(d2.script_counts, '{}'::jsonb)) AS script_entries(key, value)
                    WHERE d2.user_id = d.user_id
                      AND d2.normalized_form = d.normalized_form
                    GROUP BY key
                    ORDER BY script_total DESC, key ASC
                    LIMIT 1
                ) dominant
            ) AS dominant_script_key,
            (
                SELECT jsonb_object_agg(script_totals.script_key, script_totals.script_total)
                FROM (
                    SELECT
                        key AS script_key,
                        SUM(value::int)::int AS script_total
                    FROM lexicon_group_index_documents d2
                    CROSS JOIN LATERAL jsonb_each_text(COALESCE(d2.script_counts, '{}'::jsonb)) AS script_entries(key, value)
                    WHERE d2.user_id = d.user_id
                      AND d2.normalized_form = d.normalized_form
                    GROUP BY key
                ) AS script_totals
            ) AS script_counts
        FROM lexicon_group_index_documents d
        WHERE d.user_id = :user_id
          AND d.normalized_form = ANY(:normalized_forms)
        GROUP BY d.user_id, d.normalized_form
    ) AS slices
    LEFT JOIN LATERAL (
        SELECT COUNT(DISTINCT page_id)::int AS page_count
        FROM (
            SELECT jsonb_array_elements_text(d3.page_ids) AS page_id
            FROM lexicon_group_index_documents d3
            WHERE d3.user_id = slices.user_id
              AND d3.normalized_form = slices.normalized_form
        ) page_rows
    ) AS page_totals ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            COALESCE((
                SELECT jsonb_agg(token_value)
                FROM (
                    SELECT DISTINCT token_value
                    FROM (
                        SELECT jsonb_array_elements_text(d4.sample_tokens) AS token_value
                        FROM lexicon_group_index_documents d4
                        WHERE d4.user_id = slices.user_id
                          AND d4.normalized_form = slices.normalized_form
                    ) token_rows
                    LIMIT :sample_limit
                ) limited_tokens
            ), '[]'::jsonb) AS sample_tokens,
            COALESCE((
                SELECT jsonb_agg(context_value)
                FROM (
                    SELECT DISTINCT context_value
                    FROM (
                        SELECT jsonb_array_elements_text(d4.sample_contexts) AS context_value
                        FROM lexicon_group_index_documents d4
                        WHERE d4.user_id = slices.user_id
                          AND d4.normalized_form = slices.normalized_form
                    ) context_rows
                    LIMIT :sample_limit
                ) limited_contexts
            ), '[]'::jsonb) AS sample_contexts,
            COALESCE((
                SELECT jsonb_agg(title_value)
                FROM (
                    SELECT DISTINCT doc.title AS title_value
                    FROM lexicon_group_index_documents d5
                    JOIN documents doc ON doc.id = d5.document_id
                    WHERE d5.user_id = slices.user_id
                      AND d5.normalized_form = slices.normalized_form
                    LIMIT :sample_limit
                ) title_rows
            ), '[]'::jsonb) AS sample_document_titles
    ) AS sample_merge ON TRUE
    ON CONFLICT (user_id, normalized_form) DO UPDATE SET
        occurrence_count = EXCLUDED.occurrence_count,
        document_count = EXCLUDED.document_count,
        page_count = EXCLUDED.page_count,
        dominant_script_type = EXCLUDED.dominant_script_type,
        sample_tokens = EXCLUDED.sample_tokens,
        sample_contexts = EXCLUDED.sample_contexts,
        sample_document_titles = EXCLUDED.sample_document_titles,
        script_counts = EXCLUDED.script_counts,
        updated_at = EXCLUDED.updated_at
    """
)


def rebuild_document_slices_sql(
    session: Session,
    *,
    user_id: UUID,
    document_id: UUID,
) -> list[str]:
    session.execute(
        text(
            """
            DELETE FROM lexicon_group_index_documents
            WHERE user_id = :user_id AND document_id = :document_id
            """
        ),
        {"user_id": user_id, "document_id": document_id},
    )
    rows = session.execute(
        REBUILD_DOCUMENT_SLICES_SQL,
        {
            "user_id": user_id,
            "document_id": document_id,
            "sample_limit": SAMPLE_LIMIT,
        },
    ).all()
    session.flush()
    return [row.normalized_form for row in rows]


def rebuild_global_rows_sql(
    session: Session,
    *,
    user_id: UUID,
    normalized_forms: list[str],
) -> None:
    if not normalized_forms:
        return

    unique_forms = list(dict.fromkeys(normalized_forms))
    session.execute(
        REBUILD_GLOBAL_ROWS_SQL,
        {
            "user_id": user_id,
            "normalized_forms": unique_forms,
            "sample_limit": SAMPLE_LIMIT,
        },
    )
    session.flush()


def dominant_script_type_from_counts(script_counts: dict[str, int]) -> OccurrenceScriptType:
    if not script_counts:
        return OccurrenceScriptType.OTHER
    dominant_key = sorted(script_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    try:
        return OccurrenceScriptType(dominant_key)
    except ValueError:
        return OccurrenceScriptType.OTHER
