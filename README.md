# Armenian Historical Books OCR Backend

MVP-2 backend for authenticated uploads, page-by-page text extraction, OCR fallback with `pytesseract`, Armenian line-break reconstruction before tokenization, raw word-occurrence indexing, reviewer-centric lexicon discovery, and curated lexeme management against Supabase Postgres and Storage.

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

3. Create your environment file:

```bash
cp .env.example .env
```

4. Fill in `.env` with your Supabase URL, service role key, and database URL.
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

9. Start the Celery worker in a second terminal:

```bash
source .venv/bin/activate
cd backend
celery -A app.core.celery_app.celery_app worker -l info
```

## Full Docker Compose

If you want Redis, API, and worker under Compose:

```bash
docker compose --profile fullstack up --build
```

The compose file assumes Supabase is hosted externally and reads settings from `.env`.

## API

Base path: `/api/v1`

- `GET /health`
- `POST /documents/upload`
- `GET /documents`
- `GET /documents/{document_id}`
- `GET /documents/{document_id}/pages`
- `GET /documents/{document_id}/occurrences`
- `GET /jobs/{job_id}`
- `POST /jobs/{job_id}/retry`
- `GET /lexicon/groups`
- `GET /lexicon/groups/{normalized_form}`
- `POST /lexicon/groups/ignore`
- `POST /lexicon/groups/unignore`
- `POST /lexemes`
- `GET /lexemes`
- `GET /lexemes/{lexeme_id}`
- `PATCH /lexemes/{lexeme_id}`
- `POST /lexemes/{lexeme_id}/merge-groups`

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
```

After ingestion completes, you can browse grouped normalized forms, triage noise, and curate lexemes:

```bash
curl -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  "http://127.0.0.1:8000/api/v1/lexicon/groups?view=candidates&limit=20&offset=0"

curl -X POST "http://127.0.0.1:8000/api/v1/lexicon/groups/ignore" \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
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

5. Query `/api/v1/lexicon/groups` to inspect derived normalized-form groups built from `occurrences`.
   The default `view=candidates` queue only surfaces unreviewed Armenian groups, sorted by occurrence count.

6. Use `view=suspicious` to review Latin, mixed-script, digit-mixed, or other suspicious groups separately from the normal reviewer queue.

7. Use `/api/v1/lexicon/groups/ignore` and `/api/v1/lexicon/groups/unignore` to bulk hide or restore noise groups without deleting raw evidence.

8. Create curated lexemes with `/api/v1/lexemes` or merge additional normalized forms into an existing lexeme with `/api/v1/lexemes/{lexeme_id}/merge-groups`.

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
- lexicon groups are derived at query time by grouping `occurrences.normalized_token` for the current user.
- grouped normalized forms are the review unit surfaced in the lexicon queue.
- `lexemes` are manual editorial entities created by the user.
- `lexeme_forms` map normalized forms to curated lexemes.
- `lexicon_group_reviews` stores manual triage state such as `ignored_noise`.
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
- MVP-2 does not implement meanings, external dictionary enrichment, lemma generation, or automatic semantic merge/unmerge.
- This refinement still does not implement dictionary comparison, Nayiri matching, internet search, semantic suggestions, or automatic morphology-aware merging.
- Advanced linguistic validation and cross-page continuation handling are intentionally out of scope; only obvious same-page Armenian hyphenated line breaks are reconstructed now.

## Tests

```bash
pytest
```
