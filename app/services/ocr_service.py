from __future__ import annotations

import os
from io import BytesIO

import cv2
import numpy as np
import pytesseract
from PIL import Image

from app.core.config import Settings, get_settings
from app.core.resource_registry import ResourceRegistry, get_resource_registry
from app.utils.text_normalization import normalize_extracted_text


class OCRService:
    def __init__(self, settings: Settings | None = None, resource_registry: ResourceRegistry | None = None) -> None:
        self.settings = settings or get_settings()
        self.resource_registry = resource_registry or get_resource_registry()
        self.tessdata_prefix = self.settings.tessdata_prefix
        if not self.tessdata_prefix:
            path = self.resource_registry.local_path("hye_calfa_ocr")
            self.tessdata_prefix = str(path) if path is not None else None

    def preprocess_image(self, image_bytes: bytes) -> bytes:
        image_array = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Could not decode image for OCR preprocessing.")

        grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        denoised = cv2.fastNlMeansDenoising(grayscale, None, h=10, templateWindowSize=7, searchWindowSize=21)
        _, thresholded = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        pil_image = Image.fromarray(thresholded)
        buffer = BytesIO()
        pil_image.save(buffer, format="PNG")
        return buffer.getvalue()

    def image_to_text(self, image_bytes: bytes) -> str:
        preprocessed = self.preprocess_image(image_bytes)
        if self.tessdata_prefix:
            os.environ["TESSDATA_PREFIX"] = self.tessdata_prefix

        config_parts = [f"--dpi {self.settings.ocr_dpi}"]
        if self.tessdata_prefix:
            config_parts.append(f'--tessdata-dir "{self.tessdata_prefix}"')

        with Image.open(BytesIO(preprocessed)) as image:
            text = pytesseract.image_to_string(
                image,
                lang=self.settings.tesseract_lang,
                config=" ".join(config_parts),
            )
        return normalize_extracted_text(text)


def get_ocr_service() -> OCRService:
    return OCRService()

