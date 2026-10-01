"""Populated pre-0012 ingestion migration evidence."""

import asyncio
import sys
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Uuid, select
from sqlalchemy.orm import Session

from app.ingestion_service import new_attempt
from app.models import Chapter, ChapterFile, IngestionAttempt, Job, Page, Project, User


@pytest.fixture(scope="module", autouse=True)
def compatible_event_loop():
    if sys.platform != "win32":
        yield
        return
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)


def test_populated_legacy_ingestion_migration_preserves_history(database):
    engine, config = database
    command.downgrade(config, "0011_canonical_review_states")
    metadata = MetaData()
    metadata.reflect(bind=engine)
    # SQLite reflection loses the application UUID type (reflects CHAR(32)).
    for name in ("users", "projects", "chapters", "chapter_files", "pages", "jobs"):
        for column in metadata.tables[name].columns:
            if column.name == "id" or column.name.endswith("_id"):
                column.type = Uuid()
    users = metadata.tables["users"]
    projects = metadata.tables["projects"]
    chapters = metadata.tables["chapters"]
    files = metadata.tables["chapter_files"]
    pages = metadata.tables["pages"]
    jobs = metadata.tables["jobs"]
    user_ids = [uuid4(), uuid4()]
    project_ids = [uuid4(), uuid4(), uuid4()]
    chapter_ids = [uuid4(), uuid4(), uuid4()]
    file_ids = [uuid4(), uuid4(), uuid4()]
    job_ids = [uuid4(), uuid4(), uuid4()]
    page_ids = [uuid4(), uuid4(), uuid4()]
    with engine.begin() as connection:
        connection.execute(users.insert(), [
            {"id": user_ids[0], "email": "legacy-a@example.com", "password_hash": "hash-a"},
            {"id": user_ids[1], "email": "legacy-b@example.com", "password_hash": "hash-b"},
        ])
        connection.execute(projects.insert(), [
            {"id": project_ids[0], "user_id": user_ids[0], "name": "A"},
            {"id": project_ids[1], "user_id": user_ids[0], "name": "B"},
            {"id": project_ids[2], "user_id": user_ids[1], "name": "C"},
        ])
        connection.execute(chapters.insert(), [
            {"id": chapter_ids[0], "project_id": project_ids[0],
             "name": "Done", "status": "ready"},
            {"id": chapter_ids[1], "project_id": project_ids[1],
             "name": "Failed", "status": "failed"},
            {"id": chapter_ids[2], "project_id": project_ids[2],
             "name": "Queued", "status": "uploaded"},
        ])
        connection.execute(files.insert(), [
            {"id": file_ids[i], "chapter_id": chapter_ids[i], "original_name": f"{i}.png",
             "media_type": "image/png", "size_bytes": 3, "storage_key": f"legacy/{i}/source.png"}
            for i in range(3)
        ])
        connection.execute(jobs.insert(), [
            {"id": job_ids[0], "chapter_id": chapter_ids[0],
             "file_id": file_ids[0], "status": "completed"},
            {"id": job_ids[1], "chapter_id": chapter_ids[1],
             "file_id": file_ids[1], "status": "failed"},
            {"id": job_ids[2], "chapter_id": chapter_ids[2],
             "file_id": file_ids[2], "status": "queued"},
        ])
        connection.execute(pages.insert(), [
            {"id": page_ids[i], "chapter_id": chapter_ids[i], "page_number": 1,
             "storage_key": f"legacy/{i}/page.jpg", "thumbnail_key": f"legacy/{i}/thumb.jpg",
             "width": 10, "height": 10, "format": "JPEG", "file_size": 1, "status": "ready"}
            for i in range(3)
        ])
    with engine.connect() as connection:
        before = {table.name: len(list(connection.execute(select(table.c.id))))
                  for table in (users, projects, chapters, files, jobs, pages)}
    command.upgrade(config, "head")
    with Session(engine) as db:
        migrated = db.scalars(select(IngestionAttempt).order_by(IngestionAttempt.created_at)).all()
        assert len(db.scalars(select(User)).all()) == before["users"]
        assert len(db.scalars(select(Project)).all()) == before["projects"]
        assert len(db.scalars(select(Chapter)).all()) == before["chapters"]
        assert len(db.scalars(select(Job)).all()) == before["jobs"]
        assert len(db.scalars(select(Page)).all()) == before["pages"]
        assert len(db.scalars(select(ChapterFile)).all()) == before["chapter_files"]
        assert {a.status for a in migrated} == {"completed", "failed"}
        assert len({p.attempt_id for p in db.scalars(select(Page)).all()}) == 3
        for job_id in job_ids:
            job = db.get(Job, job_id)
            assert job.current_attempt_id is not None
            assert db.get(IngestionAttempt, job.current_attempt_id).job_id == job.id
        assert all(
            db.get(ChapterFile, file_id).storage_key.startswith("legacy/")
            for file_id in file_ids
        )
        historical_generations = {a.chapter_id: a.generation for a in migrated}
        new_job = Job(chapter_id=chapter_ids[0], file_id=file_ids[0], status="queued")
        db.add(new_job)
        db.flush()
        replacement = new_attempt(new_job, historical_generations[chapter_ids[0]] + 1)
        db.add(replacement)
        db.flush()
        new_job.current_attempt_id = replacement.id
        db.commit()
        assert replacement.id != migrated[0].id
        assert replacement.generation == historical_generations[chapter_ids[0]] + 1