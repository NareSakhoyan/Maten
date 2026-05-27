from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Text, cast, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Lexeme,
    LexemeForm,
    ReferenceEntry,
    ReferenceMatch,
    ReferenceMatchingDirection,
    ReferenceMatchRun,
    ReferenceMatchRunResult,
    ReferenceMatchRunStatus,
    ReferenceMatchStatus,
    ReferenceMatchTargetType,
    ReferenceMatchType,
    ReferenceSource,
)
from app.schemas.reference import ReferenceMatchBest


@dataclass(slots=True)
class StoredReferenceSummary:
    has_reference_match: bool
    reference_match_count: int
    best_reference_match: ReferenceMatchBest | None


@dataclass(slots=True)
class ReferenceEntryStatusSummary:
    has_reference_match: bool
    reference_match_count: int
    best_reference_match: ReferenceMatchBest | None
    latest_match_status: ReferenceMatchStatus | None
    exists_in_lexicon: bool | None
    found_in_books: bool | None
    matching_lexeme_count: int
    best_lexeme_id: UUID | None
    best_lexeme_canonical_form: str | None
    best_document_id: UUID | None
    best_document_title: str | None
    best_page_number: int | None
    best_context_snippet: str | None
    source_import_method: object | None
    source_warning: str | None


class ReferenceStatusService:
    def group_summary_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: Sequence[str],
    ) -> dict[str, StoredReferenceSummary]:
        keys = [key for key in dict.fromkeys(normalized_forms) if key]
        summaries = self._empty_summary_map(keys)
        if not keys:
            return summaries

        latest = self._latest_results_subquery(user_id)
        latest_rows = session.execute(
            select(
                latest.c.reference_entry_id,
                latest.c.source_id,
                latest.c.target_label,
                latest.c.normalized_form,
                latest.c.match_status,
                ReferenceSource.display_name.label("source_display_name"),
            )
            .join(ReferenceSource, ReferenceSource.id == latest.c.source_id)
            .where(latest.c.normalized_form.in_(keys))
        ).mappings().all()
        rows_by_key: dict[str, list[dict[str, object]]] = defaultdict(list)
        covered_keys: set[str] = set()
        for row in latest_rows:
            normalized_form = row["normalized_form"]
            if not normalized_form:
                continue
            covered_keys.add(str(normalized_form))
            if row["match_status"] == ReferenceMatchStatus.MATCHED:
                rows_by_key[normalized_form].append(row)

        for key, rows in rows_by_key.items():
            best_row = min(rows, key=self._source_first_sort_key)
            summaries[key] = StoredReferenceSummary(
                has_reference_match=True,
                reference_match_count=len(rows),
                best_reference_match=self._source_first_best_match(
                    str(best_row["source_display_name"]),
                    str(best_row["target_label"]),
                ),
            )

        for key, direct_summary in self._direct_summary_map(
            session,
            user_id=user_id,
            target_type=ReferenceMatchTargetType.LEXICON_GROUP,
            target_keys=[key for key in keys if key not in covered_keys or not summaries[key].has_reference_match],
        ).items():
            if not summaries[key].has_reference_match:
                summaries[key] = direct_summary
        for key, exact_summary in self._exact_reference_entry_summary_map(
            session,
            user_id=user_id,
            normalized_forms=[key for key in keys if not summaries[key].has_reference_match],
        ).items():
            if not summaries[key].has_reference_match:
                summaries[key] = exact_summary
        return summaries

    def lexeme_summary_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        lexeme_ids: Sequence[UUID],
    ) -> dict[str, StoredReferenceSummary]:
        keys = [str(lexeme_id) for lexeme_id in dict.fromkeys(lexeme_ids)]
        summaries = self._empty_summary_map(keys)
        if not lexeme_ids:
            return summaries

        values_by_lexeme = self._lexeme_normalized_forms_map(session, user_id=user_id, lexeme_ids=lexeme_ids)
        normalized_to_lexeme_ids: dict[str, list[str]] = defaultdict(list)
        for lexeme_id, values in values_by_lexeme.items():
            for value in values:
                normalized_to_lexeme_ids[value].append(lexeme_id)

        tracked_values = list(normalized_to_lexeme_ids)
        if not tracked_values:
            return summaries

        latest = self._latest_results_subquery(user_id)
        latest_rows = session.execute(
            select(
                latest.c.reference_entry_id,
                latest.c.target_label,
                latest.c.normalized_form,
                latest.c.match_status,
                ReferenceSource.display_name.label("source_display_name"),
            )
            .join(ReferenceSource, ReferenceSource.id == latest.c.source_id)
            .where(latest.c.normalized_form.in_(tracked_values))
        ).mappings().all()

        matched_by_lexeme: dict[str, dict[UUID, dict[str, object]]] = defaultdict(dict)
        covered_keys: set[str] = set()
        for row in latest_rows:
            normalized_form = row["normalized_form"]
            if not normalized_form:
                continue
            target_lexeme_ids = normalized_to_lexeme_ids.get(str(normalized_form), [])
            if not target_lexeme_ids:
                continue
            for lexeme_key in target_lexeme_ids:
                covered_keys.add(lexeme_key)
                if row["match_status"] != ReferenceMatchStatus.MATCHED or row["reference_entry_id"] is None:
                    continue
                matched_by_lexeme[lexeme_key][row["reference_entry_id"]] = row

        for lexeme_key, row_map in matched_by_lexeme.items():
            rows = list(row_map.values())
            if not rows:
                continue
            best_row = min(rows, key=self._source_first_sort_key)
            summaries[lexeme_key] = StoredReferenceSummary(
                has_reference_match=True,
                reference_match_count=len(rows),
                best_reference_match=self._source_first_best_match(
                    str(best_row["source_display_name"]),
                    str(best_row["target_label"]),
                ),
            )

        for key, direct_summary in self._direct_summary_map(
            session,
            user_id=user_id,
            target_type=ReferenceMatchTargetType.LEXEME,
            target_keys=[key for key in keys if key not in covered_keys],
        ).items():
            if not summaries[key].has_reference_match:
                summaries[key] = direct_summary
        unmatched_lexeme_keys = [key for key in keys if not summaries[key].has_reference_match]
        if unmatched_lexeme_keys:
            lexeme_values = {
                lexeme_id: values
                for lexeme_id, values in values_by_lexeme.items()
                if lexeme_id in unmatched_lexeme_keys
            }
            exact_by_form = self._exact_reference_entry_summary_map(
                session,
                user_id=user_id,
                normalized_forms=[value for values in lexeme_values.values() for value in values],
            )
            for lexeme_id, values in lexeme_values.items():
                matches = [exact_by_form[value] for value in values if exact_by_form.get(value, None) and exact_by_form[value].has_reference_match]
                if matches:
                    summaries[lexeme_id] = min(
                        matches,
                        key=lambda summary: (
                            summary.best_reference_match.source_display_name if summary.best_reference_match else "",
                            summary.best_reference_match.matched_form if summary.best_reference_match else "",
                        ),
                    )
        return summaries

    def reference_entry_summary_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        reference_entry_ids: Sequence[UUID],
    ) -> dict[UUID, ReferenceEntryStatusSummary]:
        keys = [entry_id for entry_id in dict.fromkeys(reference_entry_ids)]
        summaries = {
            entry_id: ReferenceEntryStatusSummary(
                has_reference_match=False,
                reference_match_count=0,
                best_reference_match=None,
                latest_match_status=None,
                exists_in_lexicon=None,
                found_in_books=None,
                matching_lexeme_count=0,
                best_lexeme_id=None,
                best_lexeme_canonical_form=None,
                best_document_id=None,
                best_document_title=None,
                best_page_number=None,
                best_context_snippet=None,
                source_import_method=None,
                source_warning=None,
            )
            for entry_id in keys
        }
        if not keys:
            return summaries

        latest = self._latest_results_subquery(user_id)
        latest_rows = session.execute(
            select(
                latest.c.reference_entry_id,
                latest.c.target_label,
                latest.c.match_status,
                latest.c.match_count,
                latest.c.exists_in_lexicon,
                latest.c.found_in_books,
                latest.c.matching_lexeme_count,
                latest.c.best_lexeme_id,
                latest.c.best_lexeme_canonical_form,
                latest.c.best_document_id,
                latest.c.best_document_title,
                latest.c.best_page_number,
                latest.c.best_context_snippet,
                latest.c.source_import_method,
                latest.c.source_warning,
                ReferenceSource.display_name.label("source_display_name"),
            )
            .join(ReferenceSource, ReferenceSource.id == latest.c.source_id)
            .where(latest.c.reference_entry_id.in_(keys))
        ).mappings().all()
        covered_keys: set[UUID] = set()
        for row in latest_rows:
            reference_entry_id = row["reference_entry_id"]
            if reference_entry_id is None:
                continue
            covered_keys.add(reference_entry_id)
            summaries[reference_entry_id] = ReferenceEntryStatusSummary(
                has_reference_match=row["match_status"] == ReferenceMatchStatus.MATCHED,
                reference_match_count=row["match_count"] if row["match_status"] == ReferenceMatchStatus.MATCHED else 0,
                best_reference_match=(
                    self._source_first_best_match(
                        str(row["source_display_name"]),
                        str(row["target_label"]),
                    )
                    if row["match_status"] == ReferenceMatchStatus.MATCHED
                    else None
                ),
                latest_match_status=row["match_status"],
                exists_in_lexicon=row["exists_in_lexicon"],
                found_in_books=row["found_in_books"],
                matching_lexeme_count=row["matching_lexeme_count"],
                best_lexeme_id=row["best_lexeme_id"],
                best_lexeme_canonical_form=row["best_lexeme_canonical_form"],
                best_document_id=row["best_document_id"],
                best_document_title=row["best_document_title"],
                best_page_number=row["best_page_number"],
                best_context_snippet=row["best_context_snippet"],
                source_import_method=row["source_import_method"],
                source_warning=row["source_warning"],
            )

        direct_rows = session.execute(
            select(ReferenceMatch, ReferenceSource.display_name)
            .join(ReferenceSource, ReferenceMatch.source_id == ReferenceSource.id)
            .where(
                ReferenceMatch.user_id == str(user_id),
                ReferenceMatch.reference_entry_id.in_(keys),
                ~ReferenceMatch.reference_entry_id.in_(covered_keys),
            )
        ).all()
        grouped_direct: dict[UUID, list[tuple[ReferenceMatch, str]]] = defaultdict(list)
        for match, display_name in direct_rows:
            if match.reference_entry_id is not None and not summaries[match.reference_entry_id].has_reference_match:
                grouped_direct[match.reference_entry_id].append((match, display_name))

        for entry_id, rows in grouped_direct.items():
            best_match_row, best_display_name = min(rows, key=lambda value: self._direct_sort_key(value[0], value[1]))
            summaries[entry_id] = ReferenceEntryStatusSummary(
                has_reference_match=True,
                reference_match_count=len(rows),
                best_reference_match=ReferenceMatchBest(
                    source_display_name=best_display_name,
                    matched_form=best_match_row.matched_form,
                    match_type=best_match_row.match_type,
                    match_score=self._float_or_none(best_match_row.match_score),
                ),
                latest_match_status=ReferenceMatchStatus.MATCHED,
                exists_in_lexicon=True,
                found_in_books=False,
                matching_lexeme_count=0,
                best_lexeme_id=None,
                best_lexeme_canonical_form=None,
                best_document_id=None,
                best_document_title=None,
                best_page_number=None,
                best_context_snippet=None,
                source_import_method=None,
                source_warning=None,
            )
        return summaries

    def matched_target_keys_subquery(self, *, user_id: UUID, target_type: ReferenceMatchTargetType):
        latest = self._latest_results_subquery(user_id)
        if target_type is ReferenceMatchTargetType.LEXICON_GROUP:
            source_first_covered = select(latest.c.normalized_form).where(latest.c.normalized_form != "").distinct()
            source_first_matched = (
                select(latest.c.normalized_form)
                .where(
                    latest.c.normalized_form != "",
                    latest.c.match_status == ReferenceMatchStatus.MATCHED,
                )
                .distinct()
            )
        elif target_type is ReferenceMatchTargetType.LEXEME:
            lexeme_forms = self._lexeme_forms_subquery(user_id=user_id)
            source_first_covered = (
                select(cast(lexeme_forms.c.lexeme_id, Text).label("target_key"))
                .join(latest, latest.c.normalized_form == lexeme_forms.c.normalized_form)
                .distinct()
            )
            source_first_matched = (
                select(cast(lexeme_forms.c.lexeme_id, Text).label("target_key"))
                .join(latest, latest.c.normalized_form == lexeme_forms.c.normalized_form)
                .where(latest.c.match_status == ReferenceMatchStatus.MATCHED)
                .distinct()
            )
        else:
            raise ValueError("Unsupported target type for matched_target_keys_subquery.")

        direct_matched = (
            select(ReferenceMatch.target_key)
            .where(
                ReferenceMatch.user_id == str(user_id),
                ReferenceMatch.target_type == target_type,
                ~ReferenceMatch.target_key.in_(source_first_covered),
            )
            .distinct()
        )
        if target_type is ReferenceMatchTargetType.LEXICON_GROUP:
            exact_reference_matched = self._exact_reference_forms_subquery(user_id=user_id)
            return source_first_matched.union(direct_matched, exact_reference_matched)
        return source_first_matched.union(direct_matched)

    def matched_reference_entry_ids_subquery(self, *, user_id: UUID, source_id: UUID | None = None):
        latest = self._latest_results_subquery(user_id)
        source_first_filters = [latest.c.reference_entry_id.is_not(None)]
        if source_id is not None:
            source_first_filters.append(latest.c.source_id == source_id)

        source_first_matched = (
            select(latest.c.reference_entry_id)
            .where(*source_first_filters, latest.c.match_status == ReferenceMatchStatus.MATCHED)
            .distinct()
        )
        source_first_covered = select(latest.c.reference_entry_id).where(*source_first_filters).distinct()
        direct_filters = [
            ReferenceMatch.user_id == str(user_id),
            ReferenceMatch.reference_entry_id.is_not(None),
            ~ReferenceMatch.reference_entry_id.in_(source_first_covered),
        ]
        if source_id is not None:
            direct_filters.append(ReferenceMatch.source_id == source_id)
        direct_matched = select(ReferenceMatch.reference_entry_id).where(*direct_filters).distinct()
        return source_first_matched.union(direct_matched)

    def _direct_summary_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        target_type: ReferenceMatchTargetType,
        target_keys: Sequence[str],
    ) -> dict[str, StoredReferenceSummary]:
        keys = [key for key in dict.fromkeys(target_keys) if key]
        summaries = self._empty_summary_map(keys)
        if not keys:
            return summaries

        rows = session.execute(
            select(ReferenceMatch, ReferenceSource.display_name)
            .join(ReferenceSource, ReferenceMatch.source_id == ReferenceSource.id)
            .where(
                ReferenceMatch.user_id == str(user_id),
                ReferenceMatch.target_type == target_type,
                ReferenceMatch.target_key.in_(keys),
            )
        ).all()
        grouped: dict[str, list[tuple[ReferenceMatch, str]]] = defaultdict(list)
        for match, display_name in rows:
            grouped[match.target_key].append((match, display_name))

        for key, values in grouped.items():
            best_match_row, best_display_name = min(values, key=lambda value: self._direct_sort_key(value[0], value[1]))
            summaries[key] = StoredReferenceSummary(
                has_reference_match=True,
                reference_match_count=len(values),
                best_reference_match=ReferenceMatchBest(
                    source_display_name=best_display_name,
                    matched_form=best_match_row.matched_form,
                    match_type=best_match_row.match_type,
                    match_score=self._float_or_none(best_match_row.match_score),
                ),
            )
        return summaries

    def _exact_reference_entry_summary_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: Sequence[str],
    ) -> dict[str, StoredReferenceSummary]:
        keys = [key for key in dict.fromkeys(normalized_forms) if key]
        summaries = self._empty_summary_map(keys)
        if not keys:
            return summaries

        rows = session.execute(
            select(ReferenceEntry.normalized_form, ReferenceEntry.surface_form, ReferenceSource.display_name)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(
                ReferenceSource.user_id == str(user_id),
                ReferenceSource.is_active.is_(True),
                ReferenceEntry.normalized_form.in_(keys),
            )
            .order_by(ReferenceSource.display_name.asc(), ReferenceEntry.surface_form.asc(), ReferenceEntry.id.asc())
        ).all()
        grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for normalized_form, surface_form, display_name in rows:
            grouped[str(normalized_form)].append((str(surface_form), str(display_name)))

        for key, values in grouped.items():
            best_surface, best_display_name = min(values, key=lambda value: (value[1], value[0]))
            summaries[key] = StoredReferenceSummary(
                has_reference_match=True,
                reference_match_count=len(values),
                best_reference_match=ReferenceMatchBest(
                    source_display_name=best_display_name,
                    matched_form=best_surface,
                    match_type=ReferenceMatchType.NORMALIZED,
                    match_score=100.0,
                ),
            )
        return summaries

    @staticmethod
    def _exact_reference_forms_subquery(*, user_id: UUID):
        return (
            select(ReferenceEntry.normalized_form)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(
                ReferenceSource.user_id == str(user_id),
                ReferenceSource.is_active.is_(True),
            )
            .distinct()
        )

    @staticmethod
    def _empty_summary_map(target_keys: Sequence[str]) -> dict[str, StoredReferenceSummary]:
        return {
            key: StoredReferenceSummary(
                has_reference_match=False,
                reference_match_count=0,
                best_reference_match=None,
            )
            for key in dict.fromkeys(target_keys)
            if key
        }

    @staticmethod
    def _source_first_best_match(source_display_name: str, matched_form: str) -> ReferenceMatchBest:
        return ReferenceMatchBest(
            source_display_name=source_display_name,
            matched_form=matched_form,
            match_type=ReferenceMatchType.NORMALIZED,
            match_score=None,
        )

    @staticmethod
    def _source_first_sort_key(row: dict[str, object]) -> tuple[str, str, str]:
        return (
            str(row["source_display_name"]),
            str(row["target_label"]),
            str(row["reference_entry_id"]),
        )

    @staticmethod
    def _direct_sort_key(match: ReferenceMatch, display_name: str) -> tuple[int, float, str, str]:
        type_priority = {
            ReferenceMatchType.EXACT: 0,
            ReferenceMatchType.NORMALIZED: 1,
            ReferenceMatchType.FUZZY: 2,
        }
        return (
            type_priority[match.match_type],
            -(float(match.match_score) if match.match_score is not None else 0.0),
            display_name,
            match.matched_form,
        )

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        return float(value) if value is not None else None

    def _latest_results_subquery(self, user_id: UUID):
        ranked = (
            select(
                ReferenceMatchRunResult.id.label("result_id"),
                ReferenceMatchRunResult.reference_entry_id.label("reference_entry_id"),
                ReferenceMatchRunResult.source_id.label("source_id"),
                ReferenceMatchRunResult.target_label.label("target_label"),
                ReferenceMatchRunResult.normalized_form.label("normalized_form"),
                ReferenceMatchRunResult.match_status.label("match_status"),
                ReferenceMatchRunResult.match_count.label("match_count"),
                ReferenceMatchRunResult.exists_in_lexicon.label("exists_in_lexicon"),
                ReferenceMatchRunResult.found_in_books.label("found_in_books"),
                ReferenceMatchRunResult.matching_lexeme_count.label("matching_lexeme_count"),
                ReferenceMatchRunResult.best_lexeme_id.label("best_lexeme_id"),
                ReferenceMatchRunResult.best_lexeme_canonical_form.label("best_lexeme_canonical_form"),
                ReferenceMatchRunResult.best_document_id.label("best_document_id"),
                ReferenceMatchRunResult.best_document_title.label("best_document_title"),
                ReferenceMatchRunResult.best_page_number.label("best_page_number"),
                ReferenceMatchRunResult.best_context_snippet.label("best_context_snippet"),
                ReferenceMatchRunResult.source_import_method.label("source_import_method"),
                ReferenceMatchRunResult.source_warning.label("source_warning"),
                func.row_number()
                .over(
                    partition_by=ReferenceMatchRunResult.reference_entry_id,
                    order_by=(
                        ReferenceMatchRun.created_at.desc(),
                        ReferenceMatchRun.id.desc(),
                        ReferenceMatchRunResult.updated_at.desc(),
                        ReferenceMatchRunResult.id.desc(),
                    ),
                )
                .label("row_num"),
            )
            .join(ReferenceMatchRun, ReferenceMatchRun.id == ReferenceMatchRunResult.run_id)
            .where(
                ReferenceMatchRun.user_id == str(user_id),
                ReferenceMatchRun.matching_direction == ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
                ReferenceMatchRun.status == ReferenceMatchRunStatus.COMPLETED,
                ReferenceMatchRunResult.user_id == str(user_id),
                ReferenceMatchRunResult.matching_direction == ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
                ReferenceMatchRunResult.target_type == ReferenceMatchTargetType.REFERENCE_ENTRY,
                ReferenceMatchRunResult.reference_entry_id.is_not(None),
            )
            .subquery()
        )
        return select(ranked).where(ranked.c.row_num == 1).subquery()

    def _lexeme_forms_subquery(self, *, user_id: UUID):
        user_key = str(user_id)
        canonical = select(
            Lexeme.id.label("lexeme_id"),
            Lexeme.canonical_normalized_form.label("normalized_form"),
        ).where(Lexeme.user_id == user_key)
        forms = select(
            LexemeForm.lexeme_id.label("lexeme_id"),
            LexemeForm.normalized_form.label("normalized_form"),
        ).where(LexemeForm.user_id == user_key)
        return canonical.union_all(forms).subquery()

    def _lexeme_normalized_forms_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        lexeme_ids: Sequence[UUID],
    ) -> dict[str, list[str]]:
        keys = list(dict.fromkeys(lexeme_ids))
        if not keys:
            return {}
        user_key = str(user_id)
        lexemes = list(
            session.scalars(
                select(Lexeme)
                .where(
                    Lexeme.user_id == user_key,
                    Lexeme.id.in_(keys),
                )
            )
        )
        result: dict[str, list[str]] = {
            str(lexeme.id): [lexeme.canonical_normalized_form]
            for lexeme in lexemes
            if lexeme.canonical_normalized_form
        }
        rows = session.execute(
            select(LexemeForm.lexeme_id, LexemeForm.normalized_form)
            .where(
                LexemeForm.user_id == user_key,
                LexemeForm.lexeme_id.in_(keys),
            )
        ).all()
        for lexeme_id, normalized_form in rows:
            bucket = result.setdefault(str(lexeme_id), [])
            if normalized_form and normalized_form not in bucket:
                bucket.append(normalized_form)
        return result


def get_reference_status_service() -> ReferenceStatusService:
    return ReferenceStatusService()
