from __future__ import annotations

from celery import Celery
from kombu import Exchange, Queue

from app.core.config import get_settings
from app.core.celery_observability import install_celery_observability


settings = get_settings()

WORKLOAD_QUEUES = (
    "ingestion",
    "ocr_cpu",
    "nlp_cpu",
    "evidence_io",
    "discovery",
    "external_io",
    "embeddings_ai_later",
)
workload_exchange = Exchange("workloads", type="direct")

celery_app = Celery(
    "armenian_books_backend",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    enable_utc=True,
    result_serializer="json",
    task_serializer="json",
    task_default_exchange="workloads",
    task_default_exchange_type="direct",
    task_default_queue="ingestion",
    task_default_routing_key="ingestion",
    task_queues=tuple(
        Queue(name, workload_exchange, routing_key=name)
        for name in WORKLOAD_QUEUES
    ),
    task_routes={
        "app.workers.tasks.process_document_ingestion": {"queue": "ingestion", "routing_key": "ingestion"},
        "app.workers.tasks.process_reference_source_import": {"queue": "evidence_io", "routing_key": "evidence_io"},
        "app.workers.tasks.process_reference_matching_run": {"queue": "evidence_io", "routing_key": "evidence_io"},
        "app.workers.tasks.process_morphology_run": {"queue": "nlp_cpu", "routing_key": "nlp_cpu"},
        "app.workers.tasks.process_document_trusted_external_lookup_run": {
            "queue": "external_io",
            "routing_key": "external_io",
        },
        "app.workers.tasks.process_document_nayiri_lookup_run": {"queue": "external_io", "routing_key": "external_io"},
        "app.workers.tasks.process_document_discovery_build": {"queue": "discovery", "routing_key": "discovery"},
        "app.workers.tasks.rebuild_lexicon_index_document": {"queue": "discovery", "routing_key": "discovery"},
        "app.workers.tasks.rebuild_lexicon_index_user": {"queue": "discovery", "routing_key": "discovery"},
    },
    task_track_started=True,
    timezone="UTC",
    worker_concurrency=settings.celery_worker_concurrency,
    worker_pool=settings.celery_worker_pool,
    worker_prefetch_multiplier=1,
)

install_celery_observability(celery_app)

