from __future__ import annotations

import hashlib


def sha256_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

