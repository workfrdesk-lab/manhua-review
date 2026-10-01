"""Persisted job state; the same processing service is used by both queues."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Job


def job_engine():
    url = get_settings().database_url.get_secret_value()
    return create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))


def process_job(job_id: str, attempt_id: str | None = None) -> None:
    if job_id.startswith("analysis:"):
        from app.analysis_service import process_analysis

        process_analysis(job_id.removeprefix("analysis:"))
        return
    if job_id.startswith("story:"):
        from app.story_service import process_story

        process_story(job_id.removeprefix("story:"))
        return
    # Legacy deliveries without an attempt identity must never claim current work.
    if attempt_id is not None:
        from app.ingestion_service import process_ingestion

        process_ingestion(UUID(job_id), UUID(attempt_id))


class JobQueue(Protocol):
    def submit(self, job_id: str, attempt_id: str | None = None) -> None: ...
    def get_status(self, job_id: str) -> str: ...
    def cancel(self, job_id: str) -> bool: ...


class LocalJobQueue:
    def submit(self, job_id: str, attempt_id: str | None = None) -> None:
        process_job(job_id, attempt_id)

    def get_status(self, job_id: str) -> str:
        if job_id.startswith("story:"):
            from app.story_service import story_queue_status

            return story_queue_status(job_id.removeprefix("story:"))
        if job_id.startswith("analysis:"):
            from app.analysis_service import analysis_queue_status

            return analysis_queue_status(job_id.removeprefix("analysis:"))
        engine = job_engine()
        try:
            with Session(engine) as db:
                job = db.get(Job, UUID(job_id))
                if job is None:
                    raise ValueError("Job not found")
                return job.status
        finally:
            engine.dispose()

    def cancel(self, job_id: str) -> bool:
        if job_id.startswith("story:"):
            from app.story_service import cancel_story

            return cancel_story(job_id.removeprefix("story:"))
        if job_id.startswith("analysis:"):
            from app.analysis_service import cancel_analysis

            return cancel_analysis(job_id.removeprefix("analysis:"))
        from app.ingestion_service import cancel_ingestion

        return cancel_ingestion(UUID(job_id))


class CeleryJobQueue(LocalJobQueue):
    def submit(self, job_id: str, attempt_id: str | None = None) -> None:
        from app.worker import celery_app

        args = [job_id, attempt_id] if attempt_id else [job_id]
        celery_app.send_task("ingestion.process", args=args, task_id=attempt_id or job_id)


def get_job_queue() -> JobQueue:
    return CeleryJobQueue() if get_settings().processing_mode == "celery" else LocalJobQueue()
