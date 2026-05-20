from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import inspect, select, text

from app.core.database import engine, session_scope
from app.db.models import Document
from app.services.lexicon_group_index_service import get_lexicon_group_index_service


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill lexicon group index rows from occurrences.")
    parser.add_argument(
        "--user-id",
        help="Optional user UUID. If omitted, rebuilds for every user with documents.",
    )
    args = parser.parse_args()

    index_service = get_lexicon_group_index_service()

    required_tables = ("lexicon_group_index", "lexicon_group_index_documents")
    existing_tables = set(inspect(engine).get_table_names())
    missing_tables = [table for table in required_tables if table not in existing_tables]
    if missing_tables:
        print("Lexicon index tables are missing:", ", ".join(missing_tables))
        print("Run database migrations first:")
        print("  cd backend")
        print("  alembic upgrade head")
        return 1

    with session_scope() as session:
        # Supabase pooler defaults to a short statement timeout; backfill needs longer.
        session.execute(text("SET LOCAL statement_timeout = '0'"))

        if args.user_id:
            user_ids = [UUID(args.user_id)]
        else:
            user_ids = list(session.scalars(select(Document.user_id).distinct()))

        total_documents = 0
        for user_id in user_ids:
            document_rows = session.execute(
                select(Document.id, Document.title).where(Document.user_id == user_id)
            ).all()
            print(f"user_id={user_id} documents={len(document_rows)} rebuilding index...", flush=True)
            rebuilt = index_service.rebuild_user(session, user_id=user_id)
            session.commit()
            total_documents += rebuilt
            print(f"user_id={user_id} documents_rebuilt={rebuilt}", flush=True)

    print(f"Backfill complete. documents_rebuilt={total_documents}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
