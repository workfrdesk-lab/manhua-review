import asyncio
import base64
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_user
from app.db import get_db
from app.ingestion import owned_page
from app.jobs import get_job_queue
from app.models import AnalysisJob, OCRResult, Panel, User, VisualAnalysis
from app.projects import protect_write
from app.storage import get_storage

router = APIRouter(prefix="/api/v1", dependencies=[Depends(protect_write)])


class AnalyzeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reading_direction: Literal["ltr", "rtl"] = "ltr"


def output(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


async def start(page_id, payload, db, user, replace=False):
    page = await owned_page(db, page_id, user)
    if page.status != "ready":
        raise HTTPException(409, "Page is not ready")
    job = await db.scalar(select(AnalysisJob).where(AnalysisJob.page_id == page_id))
    if job and not replace:
        return output(job)
    if job:
        new_id = uuid4()
        claimed = await db.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == job.id, AnalysisJob.status.in_(["completed", "failed"]))
            .values(
                id=new_id,
                status="queued",
                panel_detection_status="queued",
                ocr_status="queued",
                visual_analysis_status="queued",
                error=None,
                reading_direction=payload.reading_direction,
            )
        )
        if not claimed.rowcount:
            raise HTTPException(409, "Analysis already running")
        await db.commit()
        job = await db.get(AnalysisJob, new_id)
    else:
        job = AnalysisJob(page_id=page_id, reading_direction=payload.reading_direction)
        db.add(job)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "Analysis already requested") from None
    try:
        await asyncio.to_thread(get_job_queue().submit, "analysis:" + str(job.id))
    except Exception:
        job.status = "failed"
        job.error = "Could not submit analysis job"
        await db.commit()
        raise HTTPException(503, "Could not submit analysis job") from None
    await db.refresh(job)
    return output(job)


@router.post("/pages/{page_id}/analyze")
async def analyze(
    page_id: UUID,
    payload: AnalyzeInput = AnalyzeInput(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    return await start(page_id, payload, db, user)


@router.post("/pages/{page_id}/reanalyze")
async def reanalyze(
    page_id: UUID,
    payload: AnalyzeInput = AnalyzeInput(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    return await start(page_id, payload, db, user, replace=True)


@router.get("/pages/{page_id}/analysis-status")
async def status(
    page_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_page(db, page_id, user)
    job = await db.scalar(select(AnalysisJob).where(AnalysisJob.page_id == page_id))
    return output(job) if job else {"status": "not_started"}


@router.get("/pages/{page_id}/image")
async def page_image(
    page_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    page = await owned_page(db, page_id, user)
    try:
        data = await asyncio.to_thread(get_storage().get, page.storage_key)
    except FileNotFoundError:
        raise HTTPException(404, "Page image not found") from None
    return {"data_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}


@router.get("/pages/{page_id}/panels")
async def panels(
    page_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_page(db, page_id, user)
    records = await db.scalars(
        select(Panel).where(Panel.page_id == page_id).order_by(Panel.reading_order)
    )
    return [output(record) for record in records]


async def owned_panel(db, panel_id, user):
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(404, "Panel not found")
    await owned_page(db, panel.page_id, user)
    return panel


@router.get("/panels/{panel_id}")
async def panel(
    panel_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    return output(await owned_panel(db, panel_id, user))


@router.get("/panels/{panel_id}/ocr")
async def ocr(
    panel_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_panel(db, panel_id, user)
    rows = await db.scalars(select(OCRResult).where(OCRResult.panel_id == panel_id))
    return [output(row) for row in rows]


@router.get("/panels/{panel_id}/analysis")
async def visual(
    panel_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_panel(db, panel_id, user)
    row = await db.scalar(select(VisualAnalysis).where(VisualAnalysis.panel_id == panel_id))
    return output(row) if row else {"status": "queued"}
