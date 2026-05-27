from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import redis

from app.core.config import get_settings


logger = logging.getLogger(__name__)

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed"})


def job_progress_channel(job_id: str | UUID) -> str:
    return f"job_progress:{job_id}"


def user_job_progress_channel(user_id: str | UUID) -> str:
    return f"user_job_progress:{user_id}"


def publish_job_progress(
    job_id: str | UUID,
    payload: dict[str, Any],
    *,
    user_id: str | UUID | None = None,
) -> None:
    try:
        client = redis.from_url(get_settings().redis_url, decode_responses=True)
        message = json.dumps({**payload, "job_id": str(job_id)}, default=str)
        client.publish(job_progress_channel(job_id), message)
        if user_id is not None:
            client.publish(user_job_progress_channel(user_id), message)
        client.close()
    except Exception:
        logger.warning("Failed to publish job progress for job %s", job_id, exc_info=True)
