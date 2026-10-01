from celery import Celery

from app.config import get_settings
from app.logging_config import configure_logging

settings = get_settings()
configure_logging(settings.log_level)
celery_app = Celery("recap", broker=settings.redis_url.get_secret_value())
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    worker_hijack_root_logger=False,
    worker_prefetch_multiplier=1,
    task_default_queue="system",
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=3,
)


@celery_app.task(name="system.heartbeat")
def heartbeat() -> dict[str, str]:
    """A real operational task; not a simulated media-processing job."""
    return {"status": "alive"}


@celery_app.task(name="ingestion.process")
def ingestion_process(job_id: str, attempt_id: str | None = None) -> None:
    from app.jobs import process_job

    process_job(job_id, attempt_id)
