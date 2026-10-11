from celery import Celery

from .config import settings

celery_app = Celery(
    "fimbox",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["fimbox_api.tasks"],
)

celery_app.conf.update(
    task_default_queue=settings.celery_task_default_queue,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=settings.result_ttl_seconds,
    task_track_started=True,
    worker_concurrency=1,
    worker_pool="solo",
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "visibility_timeout": settings.visibility_timeout_seconds,
    },
    result_backend_transport_options={
        "visibility_timeout": settings.visibility_timeout_seconds,
    },
    visibility_timeout=settings.visibility_timeout_seconds,
)
