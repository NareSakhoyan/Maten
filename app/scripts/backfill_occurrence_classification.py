from __future__ import annotations

from app.core.database import session_scope
from app.services.occurrence_service import OccurrenceService


def main() -> None:
    service = OccurrenceService()
    total_backfilled = 0

    while True:
        with session_scope() as session:
            processed = service.backfill_missing_classification(session, batch_size=1000)
        total_backfilled += processed
        if processed == 0:
            break

    print(f"Backfilled occurrence classification for {total_backfilled} rows.")


if __name__ == "__main__":
    main()
