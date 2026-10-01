import asyncio
import base64
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_csrf, require_user
from app.config import get_settings
from app.db import get_db
from app.extraction import detect
from app.jobs import get_job_queue
from app.models import Chapter, ChapterFile, IngestionAttempt, Job, Page, Project, User
from app.projects import owned_chapter, protect_write
from app.storage import get_storage

router = APIRouter(prefix="/api/v1", dependencies=[Depends(protect_write)])


class PageOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    chapter_id: UUID
    page_number: int
    width: int
    height: int
    format: str
    file_size: int
    status: str
    thumbnail_url: str


class ReorderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_ids: list[UUID]


async def owned_page(db: AsyncSession, page_id: UUID, user: User) -> Page:
    # Explicit relationship join avoids exposing storage keys through a guessed UUID.
    page = await db.scalar(
        select(Page)
        .join(Page.chapter)
        .join(Project)
        .where(Page.id == page_id, Project.user_id == user.id)
    )
    if page is None:
        raise HTTPException(404, "Page not found")
    return page


def page_output(page: Page) -> PageOutput:
    return PageOutput.model_validate(
        {**page.__dict__, "thumbnail_url": f"/api/v1/pages/{page.id}/thumbnail"}
    )


@router.post("/chapters/{chapter_id}/upload")
async def upload_chapter(
    chapter_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
    _: None = Depends(require_csrf),
):
    chapter = await owned_chapter(db, chapter_id, user)
    if chapter.status != "draft":
        raise HTTPException(409, "This chapter already has an upload; create a new chapter")
    settings = get_settings()
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "File size limit exceeded")
    try:
        kind = detect(content, file.filename or "", file.content_type or "application/octet-stream")
    except ValueError as exc:
        raise HTTPException(415, str(exc)) from None
    suffix = {"pdf": "pdf", "zip": "zip", "png": "png", "jpg": "jpg"}[kind]
    key_prefix = f"users/{user.id}/projects/{chapter.project_id}/chapters/{chapter.id}"
    key = f"{key_prefix}/original/{uuid4()}.{suffix}"
    storage = get_storage()
    try:
        claimed = await db.execute(
            update(Chapter)
            .where(Chapter.id == chapter_id, Chapter.status == "draft")
            .values(status="uploaded")
        )
        if not claimed.rowcount:
            raise HTTPException(409, "Chapter already has an upload")
        storage.put(key, content, file.content_type or "application/octet-stream")
        record = ChapterFile(
            chapter_id=chapter.id,
            original_name=(file.filename or "upload")[:255],
            media_type=file.content_type or "application/octet-stream",
            size_bytes=len(content),
            storage_key=key,
        )
        db.add(record)
        chapter.status = "uploaded"
        await db.flush()
        job = Job(chapter_id=chapter.id, file_id=record.id)
        db.add(job)
        await db.flush()
        attempt = IngestionAttempt(
            job_id=job.id,
            chapter_id=chapter.id,
            generation=1,
            status="queued",
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
            last_heartbeat_at=datetime.now(timezone.utc),
        )
        db.add(attempt)
        await db.flush()
        job.current_attempt_id = attempt.id
        await db.commit()
    except Exception:
        storage.delete(key)
        raise
    queue = get_job_queue()
    try:
        await asyncio.to_thread(queue.submit, str(job.id), str(attempt.id))
    except Exception:
        raise HTTPException(503, "Could not submit processing job") from None
    await db.refresh(job)
    return {"job_id": job.id, "chapter_id": chapter.id, "status": job.status}


@router.get("/chapters/{chapter_id}/pages", response_model=list[PageOutput])
async def list_pages(
    chapter_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_chapter(db, chapter_id, user)
    pages = (
        await db.scalars(
            select(Page).where(Page.chapter_id == chapter_id).order_by(Page.page_number)
        )
    ).all()
    return [page_output(page) for page in pages]


@router.get("/pages/{page_id}", response_model=PageOutput)
async def get_page(
    page_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    return page_output(await owned_page(db, page_id, user))


@router.get("/pages/{page_id}/thumbnail")
async def thumbnail(
    page_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    page = await owned_page(db, page_id, user)
    try:
        data = get_storage().get(page.thumbnail_key)
    except FileNotFoundError:
        raise HTTPException(404, "Thumbnail not found") from None
    return {"data_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}


@router.patch("/chapters/{chapter_id}/pages/reorder")
async def reorder_pages(
    chapter_id: UUID,
    payload: ReorderInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    await owned_chapter(db, chapter_id, user)
    pages = (await db.scalars(select(Page).where(Page.chapter_id == chapter_id))).all()
    if len(payload.page_ids) != len(pages) or {p.id for p in pages} != set(payload.page_ids):
        raise HTTPException(400, "page_ids must contain every chapter page exactly once")
    # Avoid transient uniqueness collisions during a permutation.
    for number, page_id in enumerate(payload.page_ids, 1):
        await db.execute(update(Page).where(Page.id == page_id).values(page_number=-number))
    for number, page_id in enumerate(payload.page_ids, 1):
        await db.execute(update(Page).where(Page.id == page_id).values(page_number=number))
    await db.commit()
    return {"success": True}


@router.delete("/pages/{page_id}")
async def delete_page(
    page_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    page = await owned_page(db, page_id, user)
    storage = get_storage()
    storage.delete(page.storage_key)
    storage.delete(page.thumbnail_key)
    await db.delete(page)
    await db.commit()
    return {"success": True}


@router.get("/chapters/{chapter_id}/processing-status")
async def processing_status(
    chapter_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    chapter = await owned_chapter(db, chapter_id, user)
    total = await db.scalar(select(func.count(Page.id)).where(Page.chapter_id == chapter.id))
    ready = await db.scalar(
        select(func.count(Page.id)).where(Page.chapter_id == chapter.id, Page.status == "ready")
    )
    return {
        "chapter_id": chapter.id,
        "status": chapter.status,
        "total_pages": total or 0,
        "ready_pages": ready or 0,
    }


@router.post("/chapters/{chapter_id}/processing-retry")
async def retry_processing(
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
    _: None = Depends(require_csrf),
):
    chapter = await owned_chapter(db, chapter_id, user)
    job = await db.scalar(
        select(Job).where(Job.chapter_id == chapter.id).order_by(Job.created_at.desc())
    )
    if job is None or job.current_attempt_id is None:
        raise HTTPException(409, "Chapter has no processing job")
    attempt = await db.get(IngestionAttempt, job.current_attempt_id)
    if attempt is None:
        raise HTTPException(409, "Chapter has no current attempt")
    from app.ingestion_service import Fenced, recover_attempt

    job_id, attempt_id = job.id, attempt.id
    await db.rollback()  # Release the read transaction before synchronous recovery.
    try:
        new_attempt_id = await asyncio.to_thread(recover_attempt, job_id, attempt_id)
    except Fenced:
        raise HTTPException(409, "Attempt is not recoverable") from None
    queue = get_job_queue()
    try:
        await asyncio.to_thread(queue.submit, str(job_id), str(new_attempt_id))
    except Exception:
        # The DB remains authoritative and the queued attempt can be submitted again.
        raise HTTPException(503, "Could not submit recovery attempt") from None
    return {"job_id": job_id, "attempt_id": new_attempt_id}


@router.post("/chapters/{chapter_id}/processing-cleanup")
async def reconcile_processing(
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    await owned_chapter(db, chapter_id, user)
    from app.ingestion_service import DISCARD, cleanup_attempt

    ids = list(
        await db.scalars(
            select(IngestionAttempt.id).where(
                IngestionAttempt.chapter_id == chapter_id, IngestionAttempt.status.in_(DISCARD)
            )
        )
    )
    await db.rollback()
    for attempt_id in ids:
        await asyncio.to_thread(cleanup_attempt, attempt_id)
    return {"attempts_checked": len(ids)}
