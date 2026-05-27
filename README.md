# Armenian Historical Books OCR Backend

MVP-4 backend for authenticated uploads, page-by-page text extraction, OCR fallback with `pytesseract`, Armenian line-break reconstruction before tokenization, raw word-occurrence indexing, reviewer-centric lexicon discovery, curated lexeme management, personal reference-source matching, source-first reference-entry review, backend-only trusted external word lookup, backend-defined long-running job progress tracking against Supabase Postgres and Storage, and an asynchronous PIE-based morphology layer for eligible Classical Armenian sources.

## Stack

- FastAPI
- SQLAlchemy 2.x
- Alembic
- Celery + Redis
- Supabase Postgres, Storage, and Auth verification
- PyMuPDF
- Pillow + OpenCV
- pytesseract with `hye-calfa-n`

## Prerequisites

1. Python 3.12
2. Redis
3. Tesseract OCR installed locally
4. The Armenian model file `hye-calfa-n.traineddata`
5. The PIE CLI plus unpacked Classical Armenian model artifacts if you want morphology analysis

Example Ubuntu packages:

```bash
sudo apt update
sudo apt install -y python3-venv redis-server tesseract-ocr libgl1 libglib2.0-0
```

The repository already contains `../data/tessdata/hye-calfa-n.traineddata`, so the default `.env.example` points `TESSDATA_PREFIX` there.

## Setup

1. Create and activate a virtual environment:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -e ".[dev]"
```

3. Create your local secret/environment file:

```bash
cp .env.example .env
```

4. Fill in `.env` with your Supabase URL, service role key, and database URL.
   General defaults live in tracked config files:
   - `config/base.env`
   - `config/development.env`
   - `config/production.env`

   `.env` should stay focused on secrets and deploy-specific endpoints.
   For local development, prefer the Supabase Session pooler connection string if your network does not support IPv6.
   You can get it from Supabase Dashboard -> Connect.
   A typical local-safe example looks like:

```bash
DATABASE_URL=postgresql+psycopg://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

5. Run the database migration:

```bash
alembic upgrade head
```

6. Create the private Supabase storage buckets:

```bash
python -m app.scripts.bootstrap_storage
```

7. Start Redis:

```bash
docker compose up -d redis
```

8. Start the API:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

9. Start Celery workers in separate terminals by workload:

```bash
source .venv/bin/activate
cd backend
celery -A app.core.celery_app.celery_app worker -l info -Q ingestion \
  --pool="${CELERY_WORKER_POOL:-prefork}" \
  --concurrency="${CELERY_QUEUE_INGESTION_CONCURRENCY:-2}"

celery -A app.core.celery_app.celery_app worker -l info -Q ocr_cpu \
  --pool="${CELERY_WORKER_POOL:-prefork}" \
  --concurrency="${CELERY_QUEUE_OCR_CONCURRENCY:-1}"

celery -A app.core.celery_app.celery_app worker -l info -Q nlp_cpu \
  --pool="${CELERY_WORKER_POOL:-prefork}" \
  --concurrency="${CELERY_QUEUE_NLP_CONCURRENCY:-1}"

celery -A app.core.celery_app.celery_app worker -l info -Q evidence_io \
  --pool="${CELERY_WORKER_POOL:-prefork}" \
  --concurrency="${CELERY_QUEUE_EVIDENCE_CONCURRENCY:-2}"

celery -A app.core.celery_app.celery_app worker -l info -Q discovery \
  --pool="${CELERY_WORKER_POOL:-prefork}" \
  --concurrency="${CELERY_QUEUE_DISCOVERY_CONCURRENCY:-2}"

celery -A app.core.celery_app.celery_app worker -l info -Q external_io \
  --pool="${CELERY_WORKER_POOL:-prefork}" \
  --concurrency="${CELERY_QUEUE_EXTERNAL_CONCURRENCY:-1}"
```

The queue names are `ingestion`, `ocr_cpu`, `nlp_cpu`, `evidence_io`, `discovery`,
`external_io`, and the reserved `embeddings_ai_later` queue. The reserved embeddings
queue exists for future AI work only and is not used by MVP2 tasks.
Long-running tasks emit `celery_task_heartbeat` logs every `CELERY_TASK_HEARTBEAT_SECONDS`
seconds so the worker terminal shows that OCR, PIE, reference import, and discovery jobs
are still alive. Set `CELERY_TASK_HEARTBEAT_SECONDS=0` to disable heartbeat logs.

Trusted reference checks are queued as `process_document_trusted_external_lookup_run`
on `external_io`. The persisted job kind is still `nayiri_trusted_lookup` for database
compatibility, but new application code should use "trusted external" names. The old
`process_document_nayiri_lookup_run` task and `/trusted-lookups/nayiri/*` routes remain
as compatibility aliases only.

## Full Docker Compose

If you want Redis, API, and worker under Compose:

```bash
docker compose --profile fullstack up --build
```

To run only the Celery queue workers against your existing host Redis on port `6379`:

```bash
docker compose --profile workers up --build
```

This profile does not start a second Redis container. Keep your existing host Redis running so local `uvicorn` and the Docker workers share the same broker.
Compose mounts `../data` into the containers as `/data`, mounts the Nayiri corpus
into the containers as `/resources/nayiri-western-corpus`, and sets resource/model
paths for the workers:

- `PIE_MODEL_ROOT=/data/pie/xcl`
- `PIE_CLASSICAL_MODEL_PATH=/data/pie/xcl`
- `PIE_EASTERN_MODEL_PATH=/data/pie/eastern`
- `RESOURCE_PATH_NAYIRI_WESTERN_CORPUS=/resources/nayiri-western-corpus`

Set `NAYIRI_WESTERN_CORPUS_HOST_PATH` in `.env` or the shell before starting
Compose when the corpus lives somewhere else, for example in production:

```bash
NAYIRI_WESTERN_CORPUS_HOST_PATH=/srv/baghramyan/resources/nayiri-western-corpus docker compose --profile workers up --build
```

The Docker image installs the legacy `nlp-pie` CLI with curated modern dependency
pins from `requirements-pie.txt` into an isolated `/opt/pie-runtime` virtualenv;
it does not depend on a host `.pie-venv` and does not pollute the app/Celery
Python environment. Docker workers default to `PIE_ENABLED=true` and
`PIE_EXECUTABLE=/opt/pie-runtime/bin/pie`.
If you need to disable morphology in Docker, start Compose with:

```bash
DOCKER_PIE_ENABLED=false docker compose --profile workers up --build
```

If you need to point Docker at a custom PIE binary inside the container, use:

```bash
DOCKER_PIE_EXECUTABLE=/path/inside/container/to/pie docker compose --profile workers up --build
```

Your local manifest can still use host-relative paths for non-Docker runs. Docker
workers should use `RESOURCE_PATH_<RESOURCE_KEY>` container-path overrides.

The compose file assumes Supabase is hosted externally and reads settings from `.env`.

If a trusted reference job stays `running` after its worker died or was restarted,
the next start request treats it as stale after `EXTERNAL_LOOKUP_STALE_JOB_MINUTES`
(default `30`), marks it failed, and queues a fresh job.

## API

Base path: `/api/v1`

- `GET /health`
- `POST /documents/upload`
- `GET /documents`
- `GET /documents/{document_id}`
- `GET /documents/{document_id}/pages`
- `GET /documents/{document_id}/occurrences`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/events`
- `POST /jobs/{job_id}/retry`
- `GET /lexicon/groups`
- `POST /lexicon/actions`
- `GET /lexicon/groups/{normalized_form}`
- `GET /lexicon/groups/{normalized_form}/reference-matches`
- `POST /lexemes`
- `GET /lexemes`
- `GET /lexemes/{lexeme_id}`
- `GET /lexemes/{lexeme_id}/reference-matches`
- `PATCH /lexemes/{lexeme_id}`
- `POST /lexemes/{lexeme_id}/merge-groups`
- `POST /reference-sources`
- `GET /reference-sources`
- `GET /reference-sources/{source_id}`
- `GET /reference-sources/{source_id}/entries`
- `POST /reference-sources/{source_id}/import`
- `GET /reference-sources/{source_id}/imports`
- `GET /reference-sources/{source_id}/imports/{import_id}`
- `GET /reference-sources/{source_id}/imports/{import_id}/events`
- `POST /reference-matching/runs`
- `GET /reference-matching/runs`
- `GET /reference-matching/runs/{run_id}`
- `GET /reference-matching/runs/{run_id}/results`
- `GET /reference-matching/runs/{run_id}/results/{result_id}`
- `GET /reference-matching/runs/{run_id}/target-results`
- `GET /reference-matching/runs/{run_id}/target-results/{result_id}`
- `GET /reference-matching/runs/{run_id}/events`
- `POST /morphology/runs`
- `GET /morphology/runs/{run_id}`
- `GET /documents/{document_id}/morphology-summary`
- `GET /words/{normalized_form}/morphology`

## Morphology Layer

The backend keeps two separate text layers:

- `token_normalized` is the project’s existing normalization used for grouping and search.
- `lemma` and `lemma_normalized` are PIE morphology outputs and are stored separately in `morphology_analyses`.

PIE does not replace ingestion or grouping. It runs only on pre-tokenized inputs that already exist in the backend:

- document occurrences are analyzed in document/page/token order
- reference-source entries are tokenized first, then analyzed as token sequences
- large runs execute asynchronously through Celery and expose progress through the existing job endpoints

Morphology is intentionally gated:

- use `document.language_stage=classical` or `reference_source.language_stage=classical`, or set `morphology_profile=xcl_pie` for Classical DALiH/PIE
- use `language_stage=eastern` or `morphology_profile=eastern_pie` for Eastern DALiH/PIE
- if a scope is not eligible, tokens are stored as `skipped` instead of being sent to PIE

Runtime requirements for morphology:

- the worker environment must have a runnable PIE CLI binary
- `PIE_EXECUTABLE` can be either `pie` or an absolute path to the binary
- `PIE_MODEL_ROOT` defaults to Classical (`/data/pie/xcl` in Docker); Eastern uses `PIE_EASTERN_MODEL_PATH` (`/data/pie/eastern`)
- pack Eastern/Classical DALiH artifacts with `python ../scripts/pack_pie_dalih_models.py eastern` (requires `nlp-pie` / `backend/.pie-venv`)

Stored morphology fields include:

- `lemma`
- `lemma_normalized`
- `pos`
- `morph_features`
- analyzer metadata such as provider, model key, version, and status

## Upload Example

```bash
export SUPABASE_ACCESS_TOKEN="your-user-access-token"

curl -X POST "http://127.0.0.1:8000/api/v1/documents/upload" \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -F "file=@../data/pdfs/Սոմալյան,1835-17-26.pdf" \
  -F "title=Սոմալյան 1835"
```

Example follow-up requests:

```bash
curl -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  "http://127.0.0.1:8000/api/v1/documents?limit=20&offset=0"

curl -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  "http://127.0.0.1:8000/api/v1/jobs/<job_id>"

curl -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  "http://127.0.0.1:8000/api/v1/jobs/<job_id>/events"
```

After ingestion completes, you can browse grouped normalized forms, triage noise, and curate lexemes:

```bash
curl -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  "http://127.0.0.1:8000/api/v1/lexicon/groups?view=candidates&limit=20&offset=0"

curl -X POST "http://127.0.0.1:8000/api/v1/lexicon/actions" \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "ignore",
    "normalized_forms": ["xv", "abbr123"],
    "reviewer_note": "OCR noise"
  }'

curl -X POST "http://127.0.0.1:8000/api/v1/lexemes" \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "canonical_form": "Հայաստան",
    "normalized_forms": ["հայաստան", "հայաստաններ"],
    "status": "draft"
  }'

curl -X POST "http://127.0.0.1:8000/api/v1/lexemes/<lexeme_id>/merge-groups" \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"normalized_forms": ["հայկական"]}'

curl -X POST "http://127.0.0.1:8000/api/v1/reference-sources" \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "My Historical Wordlist",
    "source_type": "imported_wordlist",
    "language": "hy"
  }'
```

## Reviewer Workflow

1. Run the database migrations:

```bash
alembic upgrade head
```

2. If you upgraded an existing database and want a manual repair pass for older occurrence rows, run:

```bash
python -m app.scripts.backfill_occurrence_classification
```

3. If you need to rebuild a previously ingested document so occurrences come from reconstructed page text, run:

```bash
python -m app.scripts.reprocess_document <document_id>
```

4. Upload and ingest source books first.
   `POST /api/v1/documents/upload` returns quickly with a queued ingestion job and document metadata.
   The frontend should poll `/api/v1/jobs/{job_id}` or `/api/v1/jobs/{job_id}/events` instead of waiting for ingestion inside the upload request.

5. Query `/api/v1/lexicon/groups` to inspect derived normalized-form groups built from `occurrences`.
   The default `view=candidates` queue only surfaces unreviewed Armenian groups, sorted by occurrence count.

6. Use `view=suspicious` to review Latin, mixed-script, digit-mixed, or other suspicious groups separately from the normal reviewer queue.

7. Use `/api/v1/lexicon/actions` to bulk hide, restore, create, or merge groups without deleting raw evidence.

8. Create curated lexemes with `/api/v1/lexemes` or merge additional normalized forms into an existing lexeme with `/api/v1/lexemes/{lexeme_id}/merge-groups`.

9. Upload personal reference wordlists or dictionary extracts with `/api/v1/reference-sources` and `/api/v1/reference-sources/{source_id}/import`.
   The import endpoint stores the uploaded file, creates a queued import job, and returns immediately with the job id.
   The actual parsing, OCR fallback, normalization, and entry creation happen in the background worker.
   MVP-3 accepts `.txt`, `.csv`, `.docx`, and `.pdf` files.
   `.txt` is interpreted one entry per line.
   `.csv` requires a `surface_form` or `normalized_form` column.
   `.docx` and `.pdf` use conservative line- or paragraph-based candidate extraction rather than full dictionary parsing.

10. Optionally import pioNER named-entity surface evidence from Hugging Face.
    This imports only the `Karavet/pioNER-Armenian-Named-Entity` dataset as PER/ORG/LOC evidence.
    It does not download GloVe embeddings or enable runtime NER inference.

```bash
pip install -e ".[pioner]"
python -m app.scripts.import_pioner_dataset
```

11. Review extracted words directly from the source they came from.
    Documents expose `/api/v1/documents/{document_id}/word-candidates`.
    Reference sources expose `/api/v1/reference-sources/{source_id}/word-candidates`, and that response is anchored by the selected source rather than by a corpus-global result list.

12. Open an evidence-first drawer payload with `/api/v1/word-evidence`.
    The response consolidates traceable internal evidence rows across imported books, reference sources, and lexicon items for a normalized form.
    When `include_external=true`, it also returns trusted external evidence in a separate `external_evidence_items` array with provider labels, links, snippets, and cache metadata.

13. Search words globally with `/api/v1/words/search` and perform a quick lexicon existence check with `/api/v1/words/check`.
    Search groups results by `lexicon`, `imported_books`, `reference_sources`, and optional `trusted_external`.
    Quick word check can optionally include trusted external presence flags and an external status when `include_external=true`.

13. Check a single group or lexeme on demand with `/api/v1/lexicon/groups/{normalized_form}/reference-matches` or `/api/v1/lexemes/{lexeme_id}/reference-matches`.
    Matching supports exact, normalized, and conservative fuzzy checks.

14. Run stored batch matching with `/api/v1/reference-matching/runs`.
    Creating a run returns immediately with a queued background job and run id.
    The primary run-results view is now source-first: `/api/v1/reference-matching/runs/{run_id}/results` returns one row per imported reference entry with lexicon existence flags and imported-book evidence.
    Completed runs still persist per-target run results for audit and review, but that target-centric snapshot now lives behind `/api/v1/reference-matching/runs/{run_id}/target-results`.
    Stored results still enrich `/api/v1/lexicon/groups` and `/api/v1/lexemes`, and both list endpoints support `reference_status=matched|unmatched|all`.

## Async Jobs

- heavy operations now follow a short-lived command endpoint plus background worker pattern
- start endpoints validate input, persist lightweight state, enqueue work, and return immediately with a job payload
- ingestion upload, ingestion retry, reference import, and reference matching all follow this pattern
- jobs expose:
  - `id`
  - `job_kind`
  - `status`
  - stage/progress fields
  - `result_resource_type`
  - `result_resource_id`
  - user-facing error fields when applicable
- the frontend can poll:
  - `/api/v1/jobs/{job_id}`
  - `/api/v1/jobs/{job_id}/events`
  - `/api/v1/jobs`
- resource detail responses expose the latest related background job where useful:
  - documents include `latest_job_id` and `latest_job_status`
  - reference sources include `latest_import_job_id` and `latest_import_job_status`
- result-resource links let the frontend redirect the user after completion without guessing where the finished output lives

## Progress Tracking

- ingestion jobs, reference-source imports, and reference matching runs now expose backend-defined progress fields
- detail responses include:
  - `current_stage_code`
  - `current_stage_label`
  - `stage_message_user`
  - `progress_percent`
  - `items_processed`
  - `items_total`
- stage labels and messages are defined by the backend so the frontend does not need to infer display text from internal worker state
- progress is intentionally coarse but grounded in real work such as page processing, target loading, OCR fallback, and result saving
- event timeline endpoints expose meaningful checkpoints for polling-based UI updates without requiring WebSockets
- reference-source imports now create durable import-run records, so the latest import state is also exposed from source detail

## Reference Matching

- reference matching remains assistive metadata and background infrastructure
- matching runs still exist for traceability, progress polling, and batch refresh workflows
- reference-related matching is now source-first by default on reference routes
- reference sources are user-owned; each user has a default `manual_reference` source plus any imported sources they create
- `source_to_internal` is the default run direction for `/api/v1/reference-matching/runs`
- `source_id` is required for `source_to_internal` runs, and `target_scope` defaults to `all_internal`
- source-first runs start from one selected reference source and compare each imported reference entry against:
  - the internal lexicon
  - imported books / internal corpus occurrences
- each stored source-first run result is tied back to:
  - the originating `reference_source`
  - the exact `reference_entry`
  - the best internal evidence found so far
- the primary run-results endpoint is source-entry-centric: each row represents one imported reference entry and shows the source word, normalized form, import provenance, lexicon existence, and imported-book evidence
- source-entry run results include representative book evidence so the frontend can render matched and unmatched imported entries directly on the run page
- `internal_to_reference` still exists as an explicit secondary mode for legacy internal-first workflows
- every completed source-first matching run now stores one run-result row per imported reference entry examined
- stored target-result rows record whether the target was matched or unmatched, the match count, and a best-match summary for quick review
- stored target-result detail exposes the full match list captured for that target in the context of the run
- run detail now exposes `total_items`, `matched_items`, and `unmatched_items` so the frontend can render result summaries directly
- lightweight linking metadata is included in stored target-result rows so the frontend can jump from a run result back to the related group or lexeme view
- reference import records how a source was last imported, including whether it came from `txt`, `csv`, `docx`, `pdf_text`, or `pdf_ocr`
- exact match uses `target == surface_form`
- normalized match uses `target == normalized_form`
- fuzzy match is conservative and only runs as a lightweight assistive fallback
- scanned PDFs first try direct text extraction and then fall back to OCR when the text layer is empty or too weak
- OCR-derived reference sources expose a warning because imported entries may contain OCR noise
- exact and normalized matches are safer than fuzzy matches for OCR-derived sources
- fuzzy matching is disabled for OCR-derived (`pdf_ocr`) sources to reduce false positives
- match results are assistive metadata only
- run result summaries are review conveniences; they do not replace the underlying stored `reference_matches`
- a reference match does not create a lexeme, merge a lexeme, hide a group, or mark anything resolved automatically
- internet lookup, meaning generation, external semantic enrichment, and morphology-aware auto-merge are still out of scope

## Source-First Review

- documents now expose source-scoped grouped word candidates through `/api/v1/documents/{document_id}/word-candidates`
- source-scoped document review returns grouped normalized forms, occurrence counts, page counts, sample tokens, sample contexts, sample pages, linked lexeme metadata, suspicious flags, and reference-match summaries
- reference sources now expose imported entry review through `/api/v1/reference-sources/{source_id}/word-candidates`
- reference sources also expose raw imported-entry listing through `/api/v1/reference-sources/{source_id}/entries`
- reference-source review is reference-source-centric: the response includes a top-level source summary for the selected source plus paginated imported entries from that source only
- reference-source review keeps the imported source visible and includes import method, OCR warning metadata, and lightweight lexicon-link summaries where available
- document detail now includes workspace-style summary counts such as total candidate groups, linked groups, suspicious groups, and unmatched groups
- reference source detail now includes imported entry totals plus the latest source-first match run id/status and matched/unmatched entry counts

## Word Evidence

- `/api/v1/word-evidence` is the reusable drawer/detail payload for a normalized form
- internal evidence rows are unified across imported books, reference sources, and lexicon items
- every evidence item includes a traceable source identity, a human-readable source title, and route-hint/reference metadata where practical
- imported-book evidence includes page number, context snippet, extraction method, and occurrence id
- reference-source evidence includes source title, route hint, and import provenance metadata
- lexicon evidence includes the curated lexeme identity and optional occurrence-backed context when available
- trusted external evidence is optional and returned separately from internal evidence so provenance stays explicit
- trusted external evidence includes provider labels, matched forms, source titles, snippets when available, reference links, match type, match score, fetched timestamps, and an external status summary
- evidence payloads also expose linked lexeme summaries and related reference-match summaries when they exist

## Global Word Search

- `/api/v1/words/search` is now the main cross-source lookup entry point
- search groups results by `lexicon`, `imported_books`, `reference_sources`, and optional `trusted_external`
- search modes include `exact`, `normalized`, and conservative `fuzzy`
- `trusted_external` is opt-in through `include_categories=trusted_external` or `include_external=true`
- trusted-external groups expose `completed`, `no_results`, or `unavailable` status without breaking internal results
- `/api/v1/words/check` provides a lightweight "does this already exist?" lexicon check with optional imported-book, reference-source, and trusted-external presence flags plus `trusted_external_status`
- search prioritizes precision and traceability over broad recall

## Trusted External Lookup

- MVP-4 adds a trusted external lookup layer for words without turning the product into broad internet search
- external lookup is assistive evidence only; it does not create lexemes, mark words resolved, or make automatic lexical decisions
- the first provider is `nayiri_web`, a backend-only web adapter that fetches and parses Nayiri HTML on the server
- the frontend must never call Nayiri directly; all Nayiri access stays inside the backend provider module
- trusted external lookups are cached in `external_lookup_cache` and `external_lookup_results` so repeated searches do not re-fetch every time
- successful empty lookups are cached separately from provider failures, so `no_results` and `unavailable` stay distinct
- every external result must stay traceable and provider-labeled, with as much of the following as the provider exposes:
  - provider display name
  - matched form
  - source title
  - snippet
  - reference link
  - match type
  - match score
- provider failures fail gracefully and do not block internal search or internal evidence responses
- Nayiri lookup uses server-side throttling, bounded HTTP timeouts, and conservative HTML parsing that prefers dropping uncertain rows over returning noisy garbage
- parser tests use saved HTML fixtures rather than live network calls
- minimal external lookup configuration lives in `.env`:
  - `EXTERNAL_LOOKUP_ENABLED`
  - `EXTERNAL_LOOKUP_CACHE_TTL_HOURS`
  - `EXTERNAL_LOOKUP_HTTP_TIMEOUT_SECONDS`
  - `NAYIRI_PROVIDER_ENABLED`
  - `NAYIRI_PROVIDER_BASE_URL`
  - `NAYIRI_PROVIDER_RATE_LIMIT_MS`
- broad web search, meaning generation, and automatic lexical decisions are still out of scope for MVP-4

## Ingestion Failure Recovery

- failed ingestion jobs now store a readable `error_message_user`, an `error_code`, and `next_steps`
- retry is always manual through `POST /api/v1/jobs/{job_id}/retry`
- some failures are retryable, while others require re-uploading the original file or administrator intervention
- technical failure details remain stored backend-side in `error_message_technical` and are not exposed in the user-facing API

## Data Model Notes

- `occurrences` remain the raw source of truth produced by ingestion.
- `raw_extracted_text` preserves the extracted page text before Armenian line-break reconstruction.
- `reconstructed_text` is the reviewer-facing and tokenization-facing page text for MVP.
- `document_pages.extracted_text` is kept for backward compatibility and mirrors `reconstructed_text` going forward.
- `occurrences` are created automatically during ingestion and now include lightweight token classification metadata such as `script_type`, digit flags, and token length.
- `lexicon_group_index` and `lexicon_group_index_documents` store a denormalized read model for fast lexicon list queries; `occurrences` remain the source of truth.
- lexicon groups are always surfaced from `lexicon_group_index` (incremental updates on ingest; repair via rebuild/backfill scripts).
- grouped normalized forms are the review unit surfaced in the lexicon queue.
- `lexemes` are manual editorial entities created by the user.
- `lexeme_forms` map normalized forms to curated lexemes.
- `lexicon_group_reviews` stores manual triage state such as `ignored_noise`.
- `reference_sources` and `reference_entries` store personal imported or manual lookup material for each user.
- `reference_sources` also store last import metadata such as import method, warning text, import timestamp, and current entry count.
- `reference_source_imports` store per-import status, progress, counters, warnings, and completion state for individual reference import runs.
- `external_providers` stores the trusted external provider registry used by the backend.
- `external_lookup_cache` stores cache rows for trusted external lookups, including search mode, status, fetch time, and expiry.
- `external_lookup_results` stores traceable cached external evidence items with matched form, source title, snippet, reference link, and provider metadata.
- `reference_match_runs` now also store `matching_direction`, optional `source_id`, optional `target_scope`, and matched/unmatched summary counts for the completed run.
- `reference_matches` store assistive match results for lexicon groups and lexemes; they do not replace curation.
- `reference_match_run_results` store one run-scoped summary row per source entry for source-first runs, with lexicon and imported-book evidence summaries kept on the row for direct matched/unmatched review.
- `reference_match_run_result_matches` store the run-scoped match snapshots used by target-result detail views.
- source-first review endpoints are built on top of existing `occurrences`, `lexemes`, `lexeme_forms`, `reference_entries`, and `reference_matches` rather than a separate indexing layer.
- `job_stage_events` stores timeline checkpoints for ingestion, reference import, and reference matching jobs.
- ignored groups are hidden from the default reviewer queue but the underlying `occurrences` remain in the database.
- suspicious groups are separated from the default Armenian candidate queue; they are still available for review and can still be linked into a lexeme manually.
- assigning a normalized form to a lexeme sets `occurrences.lexeme_id` for existing matching occurrences owned by that user.
- newly ingested occurrences still start with `lexeme_id = null` until the user explicitly curates or merges groups.
- obvious same-page Armenian line-break hyphenation such as `աստու-\nած` is reconstructed before tokenization so fake split fragments are not emitted as separate occurrences.
- ingestion jobs preserve retry history through `retry_of_job_id`, so every manual retry is a separate processing attempt against the same document.

## Notes

- Table creation is migration-only. The app does not call `Base.metadata.create_all()`.
- All authenticated reads are filtered by the Supabase user ID from the bearer token.
- OCR page images are uploaded to `page-images`, original files to `book-originals`, and OCR sidecar JSON to `ocr-json`.
- PDFs use direct text extraction first. Pages without a usable text layer fall back to OCR.
- Group detail evidence includes human-readable document titles alongside internal document IDs and page numbers.
- MVP-4 does not implement broad internet search, meaning generation, external semantic enrichment beyond trusted traceable lookups, lemma generation, or automatic semantic merge/unmerge.
- Reference import is still a lightweight wordlist-style ingestion path. It does not implement full structured dictionary parsing, scholarly multi-column layout understanding, or automatic extraction of dictionary articles and definitions.
- This refinement still does not implement morphology-aware automatic merge suggestions or automatic resolution based on a trusted external hit.
- Advanced linguistic validation and cross-page continuation handling are intentionally out of scope; only obvious same-page Armenian hyphenated line breaks are reconstructed now.

## Job progress streaming

- live updates: `GET /api/v1/jobs/{job_id}/stream` (SSE)
- active jobs list: `GET /api/v1/me/active-jobs/stream` (SSE; dashboard and global job lists)
- Celery workers publish progress events to Redis; the API stream also polls Postgres as a fallback
- the web app uses SSE on job detail, document, and reference source pages instead of 3s polling when a job is active

## Job orchestration (Phase 3)

- `JobOrchestrator` centralizes Celery enqueue with `task_id=<job_id>` (idempotent retries)
- all job start/retry paths use `get_job_orchestrator().enqueue(JobKind, job_id, kwargs=...)`
- terminal progress still flows through `JobProgressService.complete` / `.fail`

## Lexicon batch actions (Phase 5)

- `POST /api/v1/lexicon/actions` — unified curation command:
  - `ignore` / `unignore` — bulk review queue changes
  - `create_lexeme` — create lexeme and link selected forms
  - `merge_into_lexeme` — link forms to an existing lexeme

## Lexicon Group Index

- always enabled (no runtime toggle)
- updated **incrementally per page** during ingestion and `reprocess_document` (`apply_page_occurrences`)
- document slices store per-document `script_counts` so global rows merge from slices without rescanning `occurrences`
- metadata refreshed when lexemes are created/merged or groups are ignored/unignored
- `rebuild_document` / `rebuild_user` are for repair and one-time backfill only
- **Phase 4 (scale path):** on PostgreSQL, full rebuilds use `INSERT … SELECT` projections. Incremental page updates still use the Python merge path.
- repair endpoints:
  - `POST /api/v1/documents/{document_id}/rebuild-index` — sync by default; `?background=true` enqueues Celery `rebuild_lexicon_index_document`
  - `POST /api/v1/lexicon/rebuild-index` — rebuild all documents for the current user (`?background=true` for async)
- backfill existing data after enabling the migration (not needed on every deploy):

```bash
alembic upgrade head
python -u -m app.scripts.backfill_lexicon_group_index
```

Optional single-user rebuild:

```bash
python -u -m app.scripts.backfill_lexicon_group_index --user-id <uuid>
```

## Production (MVP)

See [../docs/PRODUCTION.md](../docs/PRODUCTION.md) for deploy checklist, health probes, and **E2E acceptance tests**.

- `GET /api/v1/health` — liveness
- `GET /api/v1/health/ready` — readiness (Postgres + Redis)

## Tests

```bash
pytest
```
