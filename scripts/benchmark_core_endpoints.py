from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Integer, desc, func, select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.resources import resource_snapshot
from app.db.models import Document, Occurrence, OccurrenceScriptType


DEFAULT_ENDPOINT_TIMEOUT_SECONDS = 20.0
REPORT_DIR = Path(__file__).resolve().parents[1] / "benchmark-reports"


@dataclass(frozen=True)
class DocumentBenchmarkTarget:
    label: str
    document_id: str
    title: str
    original_filename: str
    page_count: int | None
    occurrence_count: int
    distinct_form_count: int
    noisy_occurrence_count: int
    sample_form: str


@dataclass(frozen=True)
class EndpointResult:
    label: str
    document_label: str | None
    method: str
    path: str
    status_code: int | None
    duration_ms: float
    response_bytes: int
    request_id: str
    error: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark core Baghramyan backend endpoints.")
    parser.add_argument("--base-url", default=os.getenv("BENCHMARK_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("BENCHMARK_AUTH_TOKEN") or os.getenv("SUPABASE_ACCESS_TOKEN"))
    parser.add_argument("--user-id", default=os.getenv("BENCHMARK_USER_ID"))
    parser.add_argument("--repetitions", type=int, default=int(os.getenv("BENCHMARK_REPETITIONS", "2")))
    parser.add_argument("--timeout", type=float, default=DEFAULT_ENDPOINT_TIMEOUT_SECONDS)
    parser.add_argument("--output-dir", default=str(REPORT_DIR))
    args = parser.parse_args()

    targets = select_targets(user_id=args.user_id)
    if not targets:
        print("No completed documents with occurrences were found.")
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = run_benchmark(
        base_url=args.base_url.rstrip("/"),
        token=args.token,
        targets=targets,
        repetitions=max(args.repetitions, 1),
        timeout=args.timeout,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "generated_at": timestamp,
        "base_url": args.base_url,
        "has_auth_token": bool(args.token),
        "resource_snapshot": resource_snapshot(),
        "targets": [asdict(target) for target in targets],
        "results": [asdict(result) for result in results],
        "summary": summarize_results(results),
    }

    json_path = output_dir / f"backend-benchmark-{timestamp}.json"
    md_path = output_dir / f"backend-benchmark-{timestamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")

    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(render_console_summary(payload))
    return 0


def select_targets(*, user_id: str | None) -> list[DocumentBenchmarkTarget]:
    with SessionLocal() as session:
        filters = []
        if user_id:
            filters.append(Document.user_id == uuid.UUID(user_id))
        rows = session.execute(
            select(
                Document.id,
                Document.title,
                Document.original_filename,
                Document.page_count,
                func.count(Occurrence.id).label("occurrence_count"),
                func.count(func.distinct(Occurrence.normalized_token)).label("distinct_form_count"),
                func.sum(
                    (
                        (Occurrence.script_type.in_(
                            [
                                OccurrenceScriptType.DIGIT_MIXED,
                                OccurrenceScriptType.MIXED,
                                OccurrenceScriptType.OTHER,
                            ]
                        ))
                        | (Occurrence.has_digits.is_(True))
                    ).cast(Integer)
                ).label("noisy_occurrence_count"),
            )
            .join(Occurrence, Occurrence.document_id == Document.id)
            .where(*filters)
            .group_by(Document.id)
            .having(func.count(Occurrence.id) > 0)
            .order_by(func.count(Occurrence.id).asc())
        ).all()

        if not rows:
            return []

        small = rows[0]
        medium = rows[len(rows) // 2]
        large_noisy = max(rows, key=lambda row: (int(row.noisy_occurrence_count or 0), int(row.occurrence_count or 0)))
        selected = [("small", small), ("medium", medium), ("large_noisy", large_noisy)]

        targets: list[DocumentBenchmarkTarget] = []
        seen: set[str] = set()
        for label, row in selected:
            document_id = str(row.id)
            if document_id in seen:
                continue
            seen.add(document_id)
            sample_form = session.scalar(
                select(Occurrence.normalized_token)
                .where(Occurrence.document_id == row.id)
                .group_by(Occurrence.normalized_token)
                .order_by(desc(func.count(Occurrence.id)), Occurrence.normalized_token.asc())
                .limit(1)
            )
            targets.append(
                DocumentBenchmarkTarget(
                    label=label,
                    document_id=document_id,
                    title=row.title,
                    original_filename=row.original_filename,
                    page_count=row.page_count,
                    occurrence_count=int(row.occurrence_count or 0),
                    distinct_form_count=int(row.distinct_form_count or 0),
                    noisy_occurrence_count=int(row.noisy_occurrence_count or 0),
                    sample_form=sample_form or "",
                )
            )
        return targets


def run_benchmark(
    *,
    base_url: str,
    token: str | None,
    targets: list[DocumentBenchmarkTarget],
    repetitions: int,
    timeout: float,
) -> list[EndpointResult]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    results: list[EndpointResult] = []
    with httpx.Client(base_url=base_url, timeout=timeout, headers=headers) as client:
        for label, path in public_endpoints():
            results.extend(hit(client, label=label, document_label=None, path=path, repetitions=repetitions))
        for target in targets:
            for label, path in document_endpoints(target):
                results.extend(hit(client, label=label, document_label=target.label, path=path, repetitions=repetitions))
    return results


def public_endpoints() -> list[tuple[str, str]]:
    return [
        ("health", "/api/v1/health"),
        ("readiness", "/api/v1/health/ready"),
    ]


def document_endpoints(target: DocumentBenchmarkTarget) -> list[tuple[str, str]]:
    q = httpx.QueryParams({"q": target.sample_form})
    word_evidence_params = httpx.QueryParams(
        {
            "normalized_form": target.sample_form,
            "source_type": "document",
            "source_id": target.document_id,
            "limit": "20",
            "offset": "0",
        }
    )
    return [
        ("document_detail", f"/api/v1/documents/{target.document_id}"),
        ("document_pages", f"/api/v1/documents/{target.document_id}/pages?limit=10&offset=0"),
        ("document_occurrences", f"/api/v1/documents/{target.document_id}/occurrences?limit=50&offset=0"),
        ("document_word_candidates", f"/api/v1/documents/{target.document_id}/word-candidates?filter=all&limit=20&offset=0"),
        ("trusted_external_summary", f"/api/v1/documents/{target.document_id}/trusted-lookups/external/summary"),
        ("discovery_candidates", f"/api/v1/documents/{target.document_id}/discovery/candidates?limit=20&offset=0"),
        ("word_evidence", f"/api/v1/word-evidence?{word_evidence_params}"),
        ("words_check", f"/api/v1/words/check?{q}"),
        (
            "words_search",
            f"/api/v1/words/search?{q}&include_lexicon=true&include_documents=true&include_reference_sources=true&limit_per_category=10",
        ),
    ]


def hit(
    client: httpx.Client,
    *,
    label: str,
    document_label: str | None,
    path: str,
    repetitions: int,
) -> list[EndpointResult]:
    results: list[EndpointResult] = []
    for _index in range(repetitions):
        request_id = f"bench-{uuid.uuid4()}"
        started_at = time.perf_counter()
        status_code: int | None = None
        response_bytes = 0
        error = None
        try:
            response = client.get(path, headers={"x-request-id": request_id})
            status_code = response.status_code
            response_bytes = len(response.content)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = (time.perf_counter() - started_at) * 1000
        results.append(
            EndpointResult(
                label=label,
                document_label=document_label,
                method="GET",
                path=path,
                status_code=status_code,
                duration_ms=round(duration_ms, 2),
                response_bytes=response_bytes,
                request_id=request_id,
                error=error,
            )
        )
    return results


def summarize_results(results: list[EndpointResult]) -> dict[str, Any]:
    grouped: dict[str, list[EndpointResult]] = {}
    for result in results:
        grouped.setdefault(f"{result.document_label or 'public'}:{result.label}", []).append(result)
    endpoint_summary = []
    for key, group in grouped.items():
        durations = [item.duration_ms for item in group]
        errors = [
            item.error or f"HTTP {item.status_code}"
            for item in group
            if item.error or (item.status_code and item.status_code >= 400)
        ]
        endpoint_summary.append(
            {
                "key": key,
                "count": len(group),
                "min_ms": min(durations),
                "median_ms": round(statistics.median(durations), 2),
                "max_ms": max(durations),
                "errors": errors,
            }
        )
    endpoint_summary.sort(key=lambda item: item["max_ms"], reverse=True)
    return {
        "top_slow_endpoints": endpoint_summary[:10],
        "status_code_counts": status_code_counts(results),
    }


def status_code_counts(results: list[EndpointResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        key = str(result.status_code or "error")
        counts[key] = counts.get(key, 0) + 1
    return counts


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Backend Benchmark Report",
        "",
        f"- Generated: `{payload['generated_at']}`",
        f"- Base URL: `{payload['base_url']}`",
        f"- Auth token provided: `{payload['has_auth_token']}`",
        f"- Resource snapshot: `{payload['resource_snapshot']}`",
        "",
        "## Documents",
        "",
    ]
    for target in payload["targets"]:
        lines.append(
            "- `{label}` `{document_id}`: {title} | pages={page_count} occurrences={occurrence_count} "
            "distinct_forms={distinct_form_count} noisy={noisy_occurrence_count} sample=`{sample_form}`".format(**target)
        )
    lines.extend(["", "## Top Slow Endpoints", ""])
    for item in payload["summary"]["top_slow_endpoints"]:
        lines.append(
            "- `{key}` count={count} min={min_ms}ms median={median_ms}ms max={max_ms}ms errors={errors}".format(**item)
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Correlate request ids from this report with backend `request_timing` and `slow_sql` logs.",
            "- Run with `BENCHMARK_AUTH_TOKEN` or `SUPABASE_ACCESS_TOKEN` for authenticated endpoints.",
            "- This benchmark does not trigger OCR, morphology, Nayiri web lookup, or discovery builds.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_console_summary(payload: dict[str, Any]) -> str:
    lines = ["Top slow endpoints:"]
    for item in payload["summary"]["top_slow_endpoints"][:5]:
        lines.append(f"- {item['key']}: max={item['max_ms']}ms median={item['median_ms']}ms errors={len(item['errors'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
