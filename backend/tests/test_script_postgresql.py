"""Opt-in disposable PostgreSQL gate; never falls back to the application DB.

Run with TEST_DATABASE_URL and ALLOW_TEST_DB_RESET=1. The existing database
fixture resets the entire target. Source deletion results compare ORM and SQL;
they characterize current constraints, not a proposed application delete policy.
"""

import os
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_ingestion_local import make_png
from test_script_finalization import enqueue, status
from test_script_finalization import harness as script_harness

from app import script_service
from app.models import (
    Base,
    OCRResult,
    Page,
    Panel,
    ScriptEvidence,
    ScriptSegment,
    ScriptVersion,
    VisualAnalysis,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="disposable PostgreSQL TEST_DATABASE_URL unavailable",
)

harness = script_harness


def test_populated_script_migration_roundtrip(harness, database):
    h = harness
    engine, config = database
    command.downgrade(config, "0012_ingestion_attempt_recovery")
    with engine.connect() as connection:
        page_ids = connection.execute(select(Page.id)).scalars().all()
        assert page_ids
    command.upgrade(config, "0013_script_domain")
    command.upgrade(config, "0014_script_jobs")
    job = enqueue(h)
    script_service.process_script(job["id"])
    assert status(h, job)["status"] == "completed"
    with Session(engine) as db:
        evidence_ids = db.scalars(select(ScriptEvidence.id)).all()
        assert evidence_ids
    command.upgrade(config, "0015_script_evidence_scope")
    command.downgrade(config, "0014_script_jobs")
    command.upgrade(config, "0015_script_evidence_scope")
    with Session(engine) as db:
        assert db.scalars(select(Page.id)).all() == page_ids
        assert db.scalars(select(ScriptEvidence.id)).all() == evidence_ids
    command.downgrade(config, "0012_ingestion_attempt_recovery")
    command.upgrade(config, "head")
    with Session(engine) as db:
        assert db.scalars(select(Page.id)).all() == page_ids


@pytest.fixture
def sources(harness):
    h = harness
    other_chapter = h.client.post(
        f"/api/v1/projects/{h.project['id']}/chapters",
        json={"name": "Other source chapter"},
        headers=h.headers,
    ).json()
    other_route = f"/api/v1/chapters/{other_chapter['id']}"
    assert (
        h.client.post(
            other_route + "/upload",
            files={"file": ("other.png", make_png(), "image/png")},
            headers=h.headers,
        ).status_code
        == 200
    )
    h.other_page = UUID(h.client.get(other_route + "/pages").json()[0]["id"])
    h.other_chapter = UUID(other_chapter["id"])
    job = enqueue(h)
    script_service.process_script(job["id"])
    assert status(h, job)["status"] == "completed"
    with Session(h.engine) as db:
        evidence = db.scalar(select(ScriptEvidence))
        page = db.get(Page, evidence.page_id)
        panels = []
        for index in range(2):
            panel = Panel(
                page_id=page.id,
                panel_index=index,
                reading_order=index,
                x=0,
                y=0,
                width=1,
                height=1,
                x_norm=0,
                y_norm=0,
                width_norm=1,
                height_norm=1,
                confidence=0.8,
            )
            db.add(panel)
            db.flush()
            panels.append(panel)
        ocr = OCRResult(panel_id=panels[0].id, text="source")
        db.add(ocr)
        db.add(VisualAnalysis(panel_id=panels[0].id))
        db.flush()
        evidence.panel_id, evidence.ocr_result_id = panels[0].id, ocr.id
        db.commit()
        return h, evidence.id, page.id, panels[0].id, panels[1].id, ocr.id


@pytest.mark.parametrize(
    "case",
    [
        "page",
        "chapter",
        "cross_chapter_page",
        "panel_page",
        "ocr_panel",
        "ocr_without_panel",
        "script_version",
        "segment_version",
        "segment_chapter",
    ],
)
def test_invalid_source_and_script_composites(sources, case):
    h, evidence_id, page_id, panel_id, other_panel, ocr_id = sources
    with Session(h.engine) as db:
        evidence = db.get(ScriptEvidence, evidence_id)
        statement = update(ScriptEvidence).where(ScriptEvidence.id == evidence_id)
        values = {
            "page": {"page_id": uuid4()},
            "chapter": {"chapter_id": h.other_chapter},
            "cross_chapter_page": {"page_id": h.other_page, "chapter_id": h.other_chapter},
            "panel_page": {"page_id": h.other_page, "chapter_id": h.other_chapter},
            "ocr_panel": {"panel_id": other_panel},
            "ocr_without_panel": {"panel_id": None},
            "script_version": {"script_version_id": uuid4()},
            "segment_version": {"segment_id": uuid4()},
            "segment_chapter": {"chapter_id": h.other_chapter},
        }[case]
        if case == "segment_chapter":
            statement = update(ScriptSegment).where(ScriptSegment.id == evidence.segment_id)
        with pytest.raises(IntegrityError):
            db.execute(statement.values(**values))
            db.flush()
        db.rollback()
        assert db.get(ScriptEvidence, evidence_id).page_id == page_id


@pytest.mark.parametrize("model", [Page, Panel, OCRResult])
@pytest.mark.parametrize("references", ["page", "panel", "ocr"])
def test_source_deletion_orm_sql_parity(sources, model, references, record_property):
    h, evidence_id, page_id, panel_id, _, ocr_id = sources
    identity = {Page: page_id, Panel: panel_id, OCRResult: ocr_id}[model]
    with Session(h.engine) as db:
        evidence = db.get(ScriptEvidence, evidence_id)
        if references != "ocr":
            evidence.ocr_result_id = None
        if references == "page":
            evidence.panel_id = None
        db.commit()
    outcomes = []
    for mode in ("sql", "orm", "orm_loaded"):
        with Session(h.engine) as db:
            try:
                if mode != "sql":
                    source = db.get(model, identity)
                    if mode == "orm_loaded":
                        panels = list(source.panels) if model is Page else []
                        if model is Panel:
                            panels = [source]
                        for panel in panels:
                            list(panel.ocr_results)
                            assert panel.visual_analysis is not None or panel.id != panel_id
                    db.delete(source)
                else:
                    db.execute(delete(model).where(model.id == identity))
                db.flush()
                db.expire_all()
                evidence = db.get(ScriptEvidence, evidence_id)
                outcomes.append(
                    (
                        "deleted",
                        evidence.page_id,
                        evidence.panel_id,
                        evidence.ocr_result_id,
                        db.get(Panel, panel_id) is not None,
                        db.get(OCRResult, ocr_id) is not None,
                        db.scalar(select(VisualAnalysis).where(VisualAnalysis.panel_id == panel_id))
                        is not None,
                    )
                )
            except IntegrityError:
                outcomes.append(("rejected",))
            finally:
                db.rollback()
            assert db.get(model, identity) is not None
    record_property(model.__tablename__ + "_delete", str(outcomes))
    assert outcomes[0] == outcomes[1] == outcomes[2]
    rejected = model is Page or (model is Panel and references == "ocr")
    assert outcomes[0][0] == ("rejected" if rejected else "deleted")


def test_script_orm_foreign_keys_match_migrations(harness):
    inspector = inspect(harness.engine)
    for model in (ScriptVersion, ScriptSegment, ScriptEvidence):
        table = Base.metadata.tables[model.__tablename__]
        expected = {
            (
                tuple(fk.column_keys),
                tuple(e.column.table.name for e in fk.elements),
                tuple(e.column.name for e in fk.elements),
                fk.ondelete or "NO ACTION",
            )
            for fk in table.foreign_key_constraints
        }
        actual = {
            (
                tuple(fk["constrained_columns"]),
                tuple(fk["referred_table"] for _ in fk["referred_columns"]),
                tuple(fk["referred_columns"]),
                fk["options"].get("ondelete", "NO ACTION"),
            )
            for fk in inspector.get_foreign_keys(table.name)
        }
        assert actual == expected
