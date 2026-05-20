from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobKind, JobStageEvent
from app.schemas.common import JobStageEventRead
from app.services.job_progress_notifier import publish_job_progress


@dataclass(frozen=True, slots=True)
class StageDefinition:
    label: str
    message: str
    default_progress_percent: int


STAGE_REGISTRIES: dict[JobKind, dict[str, StageDefinition]] = {
    JobKind.INGESTION: {
        "queued": StageDefinition("Queued", "Your document is waiting to be processed.", 0),
        "loading_source_file": StageDefinition("Preparing file", "Loading your document for processing.", 5),
        "opening_document": StageDefinition("Opening document", "Reading the document structure and pages.", 10),
        "extracting_text": StageDefinition("Extracting text", "Using embedded text where available.", 25),
        "running_ocr": StageDefinition("Running OCR", "Reading scanned pages as text.", 45),
        "reconstructing_text": StageDefinition("Reconstructing words", "Joining words that were split across lines.", 75),
        "tokenizing": StageDefinition("Building occurrences", "Collecting word occurrences with page and context.", 85),
        "saving_results": StageDefinition("Saving results", "Saving processed content for review.", 92),
        "finalizing": StageDefinition("Finalizing", "Preparing the document for the next step.", 97),
        "completed": StageDefinition("Completed", "Processing is complete.", 100),
    },
    JobKind.REFERENCE_IMPORT: {
        "queued": StageDefinition("Queued", "Your reference import is waiting to start.", 0),
        "reading_source_file": StageDefinition("Reading file", "Opening the reference file for import.", 10),
        "extracting_entries": StageDefinition("Extracting entries", "Collecting likely reference entries from the file.", 30),
        "running_ocr": StageDefinition("Running OCR", "Reading scanned reference pages as text.", 45),
        "normalizing_entries": StageDefinition("Normalizing entries", "Normalizing extracted entries for matching.", 70),
        "saving_source": StageDefinition("Saving source", "Saving reference entries to your source.", 88),
        "finalizing": StageDefinition("Finalizing", "Preparing the imported reference source for use.", 96),
        "completed": StageDefinition("Completed", "Reference import is complete.", 100),
    },
    JobKind.REFERENCE_MATCHING: {
        "queued": StageDefinition("Queued", "Your matching run is waiting to start.", 0),
        "loading_targets": StageDefinition("Loading targets", "Collecting groups and lexemes to compare.", 10),
        "loading_reference_sources": StageDefinition("Loading reference sources", "Loading your reference entries for comparison.", 20),
        "loading_reference_entries": StageDefinition("Loading source entries", "Loading reference entries from the selected source.", 20),
        "running_exact_match": StageDefinition("Running exact match", "Checking for direct surface-form matches.", 35),
        "running_normalized_match": StageDefinition("Running normalized match", "Checking normalized forms for likely known words.", 55),
        "running_fuzzy_match": StageDefinition("Running fuzzy match", "Checking conservative fuzzy matches for remaining items.", 75),
        "checking_lexicon": StageDefinition("Checking lexicon", "Checking imported reference entries against your internal lexicon.", 40),
        "checking_imported_books": StageDefinition("Checking imported books", "Checking imported reference entries against your books.", 65),
        "saving_matches": StageDefinition("Saving matches", "Saving match results for review.", 90),
        "saving_results": StageDefinition("Saving results", "Saving source-entry results for review.", 90),
        "finalizing": StageDefinition("Finalizing", "Preparing the run results for display.", 97),
        "completed": StageDefinition("Completed", "Reference matching is complete.", 100),
    },
    JobKind.MORPHOLOGY: {
        "queued": StageDefinition("Queued", "Your morphology run is waiting to start.", 0),
        "loading_scope": StageDefinition("Loading scope", "Collecting tokens from the selected source.", 5),
        "checking_eligibility": StageDefinition(
            "Checking eligibility",
            "Checking whether the selected source can use the Classical Armenian PIE model.",
            15,
        ),
        "running_pie": StageDefinition("Running PIE", "Analyzing eligible tokens with the PIE model.", 55),
        "saving_results": StageDefinition("Saving results", "Saving morphology results for review.", 90),
        "finalizing": StageDefinition("Finalizing", "Preparing the morphology results for use.", 97),
        "completed": StageDefinition("Completed", "Morphology analysis is complete.", 100),
    },
}


class JobProgressService:
    def set_stage(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
        stage_code: str,
        message_user: str | None = None,
        progress_percent: int | None = None,
        items_processed: int | None = None,
        items_total: int | None = None,
        append_event: bool = True,
        force_event: bool = False,
    ) -> None:
        stage = self._stage_definition(job_kind, stage_code)
        previous_stage_code = getattr(job, "current_stage_code", None)
        previous_stage_label = getattr(job, "current_stage_label", None)
        stage_changed = previous_stage_code != stage_code or previous_stage_label != stage.label

        setattr(job, "current_stage_code", stage_code)
        setattr(job, "current_stage_label", stage.label)
        setattr(job, "stage_message_user", message_user or stage.message)
        if hasattr(job, "step"):
            setattr(job, "step", stage_code)

        if progress_percent is not None:
            setattr(job, "progress_percent", self._clamp_progress(progress_percent))
        elif stage_changed or getattr(job, "progress_percent", None) is None:
            setattr(job, "progress_percent", stage.default_progress_percent)

        if items_processed is not None:
            setattr(job, "items_processed", items_processed)
        if items_total is not None:
            setattr(job, "items_total", items_total)

        if append_event and (force_event or stage_changed):
            self.append_event(
                session,
                job_kind=job_kind,
                job=job,
                stage_code=stage_code,
                stage_label=stage.label,
                message_user=getattr(job, "stage_message_user", None),
                progress_percent=getattr(job, "progress_percent", None),
                items_processed=getattr(job, "items_processed", None),
                items_total=getattr(job, "items_total", None),
            )

    def update_progress(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
        progress_percent: int | None = None,
        items_processed: int | None = None,
        items_total: int | None = None,
        message_user: str | None = None,
        append_event: bool = False,
        force_event: bool = False,
    ) -> None:
        if progress_percent is not None:
            setattr(job, "progress_percent", self._clamp_progress(progress_percent))
        if items_processed is not None:
            setattr(job, "items_processed", items_processed)
        if items_total is not None:
            setattr(job, "items_total", items_total)
        if message_user is not None:
            setattr(job, "stage_message_user", message_user)

        if append_event:
            self.append_event(
                session,
                job_kind=job_kind,
                job=job,
                stage_code=getattr(job, "current_stage_code", "queued") or "queued",
                stage_label=getattr(job, "current_stage_label", "Queued") or "Queued",
                message_user=getattr(job, "stage_message_user", None),
                progress_percent=getattr(job, "progress_percent", None),
                items_processed=getattr(job, "items_processed", None),
                items_total=getattr(job, "items_total", None),
                force=force_event,
            )

    def complete(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
        stage_code: str = "completed",
        message_user: str | None = None,
    ) -> None:
        self.set_stage(
            session,
            job_kind=job_kind,
            job=job,
            stage_code=stage_code,
            message_user=message_user,
            progress_percent=100,
            append_event=True,
            force_event=True,
        )
        if hasattr(job, "finished_at"):
            setattr(job, "finished_at", datetime.now(timezone.utc))
        publish_job_progress(str(getattr(job, "id")), {"type": "job_refresh"}, user_id=str(getattr(job, "user_id")))

    def fail(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
        message_user: str | None = None,
    ) -> None:
        if hasattr(job, "finished_at"):
            setattr(job, "finished_at", datetime.now(timezone.utc))
        self.append_event(
            session,
            job_kind=job_kind,
            job=job,
            stage_code=getattr(job, "current_stage_code", "failed") or "failed",
            stage_label=getattr(job, "current_stage_label", "Failed") or "Failed",
            message_user=message_user or getattr(job, "stage_message_user", None),
            progress_percent=getattr(job, "progress_percent", None),
            items_processed=getattr(job, "items_processed", None),
            items_total=getattr(job, "items_total", None),
            force=True,
        )
        publish_job_progress(str(getattr(job, "id")), {"type": "job_refresh"}, user_id=str(getattr(job, "user_id")))

    def append_event(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
        stage_code: str,
        stage_label: str,
        message_user: str | None,
        progress_percent: int | None,
        items_processed: int | None,
        items_total: int | None,
        force: bool = False,
    ) -> None:
        job_id = str(getattr(job, "id"))
        user_id = str(getattr(job, "user_id"))
        if not force:
            latest_event = session.scalar(
                select(JobStageEvent)
                .where(
                    JobStageEvent.job_kind == job_kind,
                    JobStageEvent.job_id == job_id,
                )
                .order_by(JobStageEvent.created_at.desc(), JobStageEvent.id.desc())
                .limit(1)
            )
            if (
                latest_event is not None
                and latest_event.stage_code == stage_code
                and latest_event.progress_percent == progress_percent
                and latest_event.items_processed == items_processed
                and latest_event.items_total == items_total
                and latest_event.message_user == message_user
            ):
                return

        event = JobStageEvent(
            job_kind=job_kind,
            job_id=job_id,
            user_id=user_id,
            stage_code=stage_code,
            stage_label=stage_label,
            message_user=message_user,
            progress_percent=progress_percent,
            items_processed=items_processed,
            items_total=items_total,
            created_at=datetime.now(timezone.utc),
        )
        session.add(event)
        session.flush()
        publish_job_progress(
            job_id,
            {
                "type": "event",
                "event": JobStageEventRead.model_validate(event).model_dump(mode="json"),
            },
            user_id=user_id,
        )

    def list_events(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job_id: UUID | str,
        user_id: UUID | str,
    ) -> list[JobStageEvent]:
        return list(
            session.scalars(
                select(JobStageEvent)
                .where(
                    JobStageEvent.job_kind == job_kind,
                    JobStageEvent.job_id == str(job_id),
                    JobStageEvent.user_id == str(user_id),
                )
                .order_by(JobStageEvent.created_at.asc(), JobStageEvent.id.asc())
            )
        )

    @staticmethod
    def ranged_progress(processed: int, total: int, *, start_percent: int, end_percent: int) -> int:
        if total <= 0:
            return start_percent
        span = max(0, end_percent - start_percent)
        ratio = min(max(processed / total, 0.0), 1.0)
        return start_percent + int(span * ratio)

    @staticmethod
    def _clamp_progress(value: int) -> int:
        return max(0, min(100, int(value)))

    @staticmethod
    def _stage_definition(job_kind: JobKind, stage_code: str) -> StageDefinition:
        registry = STAGE_REGISTRIES.get(job_kind, {})
        if stage_code in registry:
            return registry[stage_code]
        fallback_label = stage_code.replace("_", " ").strip().title() or "Processing"
        return StageDefinition(label=fallback_label, message="", default_progress_percent=0)


def get_job_progress_service() -> JobProgressService:
    return JobProgressService()
