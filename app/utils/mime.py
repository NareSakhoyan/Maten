from __future__ import annotations

import mimetypes


PDF_MIME_TYPE = "application/pdf"
SUPPORTED_IMAGE_MIME_TYPES = {
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
}
SUPPORTED_UPLOAD_MIME_TYPES = {PDF_MIME_TYPE, *SUPPORTED_IMAGE_MIME_TYPES}


def detect_mime_type(filename: str, data: bytes, provided_mime_type: str | None = None) -> str:
    provided = (provided_mime_type or "").split(";", maxsplit=1)[0].strip().lower()

    if data.startswith(b"%PDF-"):
        return PDF_MIME_TYPE
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"

    if provided in SUPPORTED_UPLOAD_MIME_TYPES:
        return provided

    guessed, _ = mimetypes.guess_type(filename)
    if guessed in SUPPORTED_UPLOAD_MIME_TYPES:
        return guessed

    raise ValueError("Unsupported file type. Only PDF and image uploads are accepted.")


def is_pdf_mime(mime_type: str) -> bool:
    return mime_type == PDF_MIME_TYPE


def is_image_mime(mime_type: str) -> bool:
    return mime_type in SUPPORTED_IMAGE_MIME_TYPES

