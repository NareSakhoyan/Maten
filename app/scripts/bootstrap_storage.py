from __future__ import annotations

import httpx

from app.services.storage_service import get_storage_service


def main() -> int:
    storage_service = get_storage_service()
    settings = storage_service.settings
    bucket_names = [
        settings.supabase_bucket_book_originals,
        settings.supabase_bucket_page_images,
        settings.supabase_bucket_ocr_json,
    ]

    try:
        for bucket_name in bucket_names:
            created = storage_service.create_bucket_if_missing(bucket_name, public=False)
            status_text = "created" if created else "exists"
            print(f"{bucket_name}: {status_text}")
    except httpx.ConnectError as exc:
        raise SystemExit(
            "Could not connect to Supabase Storage.\n"
            "Check `SUPABASE_URL` in `backend/.env` and make sure it is your real project URL,\n"
            "for example `https://<project-ref>.supabase.co`.\n"
            "Also confirm you have internet access from this machine."
        ) from exc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
