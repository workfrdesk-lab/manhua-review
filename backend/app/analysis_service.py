"""One-page pipeline; providers never execute in API routes."""

import io
import logging
import time
from uuid import UUID

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.analysis_providers import (
    BasicVisualAnalyzer,
    OpenCVPanelDetector,
    PaddleOCRProvider,
    full_page,
    group_text,
    reading_order,
)
from app.jobs import job_engine
from app.models import AnalysisJob, OCRResult, Page, Panel, VisualAnalysis
from app.storage import get_storage

logger = logging.getLogger(__name__)


def providers():
    return OpenCVPanelDetector(), PaddleOCRProvider(), BasicVisualAnalyzer()


def log_stage(job, stage, start, status, error=None):
    logger.info(
        "analysis_stage",
        extra={
            "page_id": str(job.page_id),
            "job_id": str(job.id),
            "stage": stage,
            "duration": round(time.monotonic() - start, 3),
            "status": status,
            "error": error,
        },
    )


def process_analysis(job_id: str) -> None:
    engine = job_engine()
    try:
        with Session(engine) as db:
            claim = db.execute(
                update(AnalysisJob)
                .where(AnalysisJob.id == UUID(job_id), AnalysisJob.status == "queued")
                .values(status="processing")
            )
            db.commit()
            if not claim.rowcount:
                return
            job = db.get(AnalysisJob, UUID(job_id))
            try:
                page = db.get(Page, job.page_id)
                with Image.open(io.BytesIO(get_storage().get(page.storage_key))) as source:
                    image = source.convert("RGB")
                try:
                    run_stages(db, job, image)
                finally:
                    image.close()
            except Exception as exc:
                db.rollback()
                job = db.get(AnalysisJob, UUID(job_id))
                if job:
                    job.status = "failed"
                    job.error = "Page analysis failed; see server logs"
                    for stage in ("panel_detection", "ocr", "visual_analysis"):
                        if getattr(job, stage + "_status") in {"queued", "processing"}:
                            setattr(job, stage + "_status", "failed")
                    db.commit()
                    logger.exception(
                        "analysis_failed",
                        extra={"page_id": str(job.page_id), "error": type(exc).__name__},
                    )
    finally:
        engine.dispose()


def run_stages(db, job, image):
    detector, ocr, analyzer = providers()
    start = time.monotonic()
    job.panel_detection_status = "processing"
    db.commit()
    detection_error = None
    try:
        detected = detector.detect(image) or full_page(image)
    except Exception as exc:
        detected = full_page(image)
        detection_error = type(exc).__name__
    detected = reading_order(detected, job.reading_direction)
    # Old results replaced together after successful load/detection, using ORM cascades
    # also on SQLite connections that do not enable foreign-key cascades.
    for old in db.scalars(select(Panel).where(Panel.page_id == job.page_id)).all():
        db.delete(old)
    db.flush()
    panels = []
    for index, box in enumerate(detected, 1):
        panel = Panel(
            page_id=job.page_id,
            panel_index=index,
            reading_order=index,
            x=box.x,
            y=box.y,
            width=box.width,
            height=box.height,
            x_norm=box.x / image.width,
            y_norm=box.y / image.height,
            width_norm=box.width / image.width,
            height_norm=box.height / image.height,
            confidence=box.confidence,
            status=box.kind,
        )
        db.add(panel)
        panels.append(panel)
    job.panel_detection_status = "completed"
    db.commit()
    log_stage(job, "panel_detection", start, "completed", detection_error)
    for stage in ("ocr", "visual_analysis"):
        start = time.monotonic()
        setattr(job, stage + "_status", "processing")
        db.commit()
        failed = False
        for panel in panels:
            with image.crop(
                (panel.x, panel.y, panel.x + panel.width, panel.y + panel.height)
            ) as crop:
                try:
                    if stage == "ocr":
                        result = ocr.extract(crop)
                        bubbles = group_text(result.regions, job.reading_direction)
                        confidence = min((r["confidence"] for r in result.regions), default=1.0)
                        record = OCRResult(
                            panel_id=panel.id,
                            text="\n\n".join(b["text"] for b in bubbles),
                            language=result.language,
                            confidence=confidence,
                            status="needs_review"
                            if result.regions and confidence < 0.65
                            else "completed",
                            data_json={
                                "regions": result.regions,
                                "bubbles": bubbles,
                                "raw": result.raw,
                                "coordinate_space": "panel_pixels",
                            },
                        )
                    else:
                        record = VisualAnalysis(panel_id=panel.id, data_json=analyzer.analyze(crop))
                    db.add(record)
                except Exception as exc:
                    failed = True
                    error = type(exc).__name__
                    if stage == "ocr":
                        db.add(
                            OCRResult(
                                panel_id=panel.id,
                                text="",
                                confidence=0,
                                status="failed",
                                data_json={"error": "OCR provider unavailable or failed"},
                            )
                        )
                    else:
                        db.add(
                            VisualAnalysis(
                                panel_id=panel.id,
                                status="failed",
                                data_json={"error": "Visual provider failed"},
                            )
                        )
                    log_stage(job, stage, start, "failed", error)
                db.commit()
        setattr(job, stage + "_status", "failed" if failed else "completed")
        db.commit()
        log_stage(job, stage, start, "failed" if failed else "completed")
    job.status = (
        "failed" if "failed" in (job.ocr_status, job.visual_analysis_status) else "completed"
    )
    job.error = (
        "Some stages failed; successful results are retained" if job.status == "failed" else None
    )
    db.commit()


def analysis_queue_status(job_id):
    engine = job_engine()
    try:
        with Session(engine) as db:
            job = db.get(AnalysisJob, UUID(job_id))
            if not job:
                raise ValueError("Job not found")
            return job.status
    finally:
        engine.dispose()


def cancel_analysis(job_id):
    engine = job_engine()
    try:
        with Session(engine) as db:
            result = db.execute(
                update(AnalysisJob)
                .where(AnalysisJob.id == UUID(job_id), AnalysisJob.status == "queued")
                .values(
                    status="failed",
                    error="Cancelled before processing",
                    panel_detection_status="failed",
                    ocr_status="failed",
                    visual_analysis_status="failed",
                )
            )
            db.commit()
            return bool(result.rowcount)
    finally:
        engine.dispose()
