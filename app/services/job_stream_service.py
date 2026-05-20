from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import redis
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.schemas.common import JobStageEventRead
from app.schemas.job import LongRunningJobRead
from app.services.job_progress_notifier import (
    TERMINAL_JOB_STATUSES,
    job_progress_channel,
    user_job_progress_channel,
)
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service


def format_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


class JobStreamService:
    def __init__(
        self,
        *,
        long_running_job_service: LongRunningJobService | None = None,
        job_progress_service: JobProgressService | None = None,
    ) -> None:
        self.long_running_job_service = long_running_job_service or get_long_running_job_service()
        self.job_progress_service = job_progress_service or get_job_progress_service()

    def load_snapshot(self, session: Session, *, user_id: UUID, job_id: UUID) -> dict[str, Any] | None:
        job = self.long_running_job_service.get_user_job(session, user_id=user_id, job_id=job_id)
        if job is None:
            return None

        events = self.job_progress_service.list_events(
            session,
            job_kind=job.job_kind,
            job_id=job_id,
            user_id=user_id,
        )
        return {
            "job": job.model_dump(mode="json"),
            "events": [JobStageEventRead.model_validate(event).model_dump(mode="json") for event in events],
        }

    async def stream(
        self,
        *,
        user_id: UUID,
        job_id: UUID,
        poll_interval_seconds: float = 2.0,
    ) -> AsyncIterator[str]:
        with SessionLocal() as session:
            snapshot = self.load_snapshot(session, user_id=user_id, job_id=job_id)
        if snapshot is None:
            yield format_sse("error", {"detail": "Job not found."})
            return

        yield format_sse("snapshot", snapshot)

        if snapshot["job"]["status"] in TERMINAL_JOB_STATUSES:
            yield format_sse("done", {"status": snapshot["job"]["status"]})
            return

        seen_event_ids = {event["id"] for event in snapshot["events"]}
        stop_event = asyncio.Event()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def redis_listener() -> None:
            client = redis.from_url(get_settings().redis_url, decode_responses=True)
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(job_progress_channel(job_id))
            try:
                while not stop_event.is_set():
                    message = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue
                    data = message.get("data")
                    if not data:
                        continue
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    await queue.put(payload)
            finally:
                pubsub.close()
                client.close()

        async def db_poller() -> None:
            while not stop_event.is_set():
                await asyncio.sleep(poll_interval_seconds)
                with SessionLocal() as session:
                    current = self.load_snapshot(session, user_id=user_id, job_id=job_id)
                if current is None:
                    await queue.put({"type": "error", "detail": "Job not found."})
                    return

                job = current["job"]
                for event in current["events"]:
                    if event["id"] not in seen_event_ids:
                        seen_event_ids.add(event["id"])
                        await queue.put({"type": "event", "event": event})

                await queue.put({"type": "job", "job": job})
                if job["status"] in TERMINAL_JOB_STATUSES:
                    await queue.put({"type": "done", "status": job["status"]})
                    return

        listener_task = asyncio.create_task(redis_listener())
        poller_task = asyncio.create_task(db_poller())

        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    yield format_sse("ping", {"at": datetime.now(timezone.utc).isoformat()})
                    continue

                message_type = payload.get("type")
                if message_type == "error":
                    yield format_sse("error", {"detail": payload.get("detail", "Job not found.")})
                    break
                if message_type == "done":
                    yield format_sse("done", {"status": payload.get("status")})
                    break
                if message_type == "job_refresh":
                    with SessionLocal() as session:
                        current = self.load_snapshot(session, user_id=user_id, job_id=job_id)
                    if current is None:
                        yield format_sse("error", {"detail": "Job not found."})
                        break
                    job = current["job"]
                    for event in current["events"]:
                        if event["id"] not in seen_event_ids:
                            seen_event_ids.add(event["id"])
                            yield format_sse("event", {"event": event})
                    yield format_sse("job", {"job": job})
                    if job["status"] in TERMINAL_JOB_STATUSES:
                        yield format_sse("done", {"status": job["status"]})
                        break
                    continue
                if message_type == "job":
                    job = payload["job"]
                    yield format_sse("job", {"job": job})
                    if job["status"] in TERMINAL_JOB_STATUSES:
                        yield format_sse("done", {"status": job["status"]})
                        break
                    continue
                if message_type == "event":
                    event = payload["event"]
                    seen_event_ids.add(event["id"])
                    yield format_sse("event", {"event": event})
                    continue
        finally:
            stop_event.set()
            listener_task.cancel()
            poller_task.cancel()
            await asyncio.gather(listener_task, poller_task, return_exceptions=True)

    def load_active_jobs_snapshot(self, session: Session, *, user_id: UUID) -> list[dict[str, Any]]:
        jobs = self.long_running_job_service.list_active_jobs(session, user_id=user_id)
        return [job.model_dump(mode="json") for job in jobs]

    async def stream_active_jobs(
        self,
        *,
        user_id: UUID,
        poll_interval_seconds: float = 3.0,
    ) -> AsyncIterator[str]:
        with SessionLocal() as session:
            jobs = self.load_active_jobs_snapshot(session, user_id=user_id)
        yield format_sse("snapshot", {"jobs": jobs})

        stop_event = asyncio.Event()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def redis_listener() -> None:
            client = redis.from_url(get_settings().redis_url, decode_responses=True)
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(user_job_progress_channel(user_id))
            try:
                while not stop_event.is_set():
                    message = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue
                    data = message.get("data")
                    if not data:
                        continue
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    await queue.put(payload)
            finally:
                pubsub.close()
                client.close()

        async def db_poller() -> None:
            while not stop_event.is_set():
                await asyncio.sleep(poll_interval_seconds)
                with SessionLocal() as session:
                    jobs = self.load_active_jobs_snapshot(session, user_id=user_id)
                await queue.put({"type": "jobs", "jobs": jobs})

        listener_task = asyncio.create_task(redis_listener())
        poller_task = asyncio.create_task(db_poller()) if jobs else None

        async def refresh_active_jobs() -> None:
            with SessionLocal() as session:
                jobs = self.load_active_jobs_snapshot(session, user_id=user_id)
            await queue.put({"type": "jobs", "jobs": jobs})

        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    yield format_sse("ping", {"at": datetime.now(timezone.utc).isoformat()})
                    continue

                message_type = payload.get("type")
                if message_type == "jobs":
                    yield format_sse("jobs", {"jobs": payload.get("jobs", [])})
                    continue

                job_id_raw = payload.get("job_id")
                if not job_id_raw:
                    await refresh_active_jobs()
                    continue

                try:
                    job_id = UUID(str(job_id_raw))
                except ValueError:
                    await refresh_active_jobs()
                    continue

                if message_type == "job_refresh":
                    await refresh_active_jobs()
                    continue

                with SessionLocal() as session:
                    job = self.long_running_job_service.get_user_job(
                        session,
                        user_id=user_id,
                        job_id=job_id,
                    )
                if job is None:
                    await refresh_active_jobs()
                    continue

                job_payload = job.model_dump(mode="json")
                yield format_sse("job", {"job": job_payload})
                if job.status in TERMINAL_JOB_STATUSES:
                    await refresh_active_jobs()
        finally:
            stop_event.set()
            listener_task.cancel()
            if poller_task is not None:
                poller_task.cancel()
            tasks = [listener_task]
            if poller_task is not None:
                tasks.append(poller_task)
            await asyncio.gather(*tasks, return_exceptions=True)


def get_job_stream_service() -> JobStreamService:
    return JobStreamService()
