from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_csrf, require_origin, require_user
from app.db import get_db
from app.models import Chapter, Project, User


async def protect_write(request: Request, db: AsyncSession = Depends(get_db)):
    require_origin(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        await require_csrf(request, db)


router = APIRouter(prefix="/api/v1", dependencies=[Depends(protect_write)])


class NameInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)


class ResourceOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    status: str
    created_at: datetime
    updated_at: datetime


class ProjectOutput(ResourceOutput):
    user_id: UUID


class ChapterOutput(ResourceOutput):
    project_id: UUID


async def owned_project(db: AsyncSession, project_id: UUID, user: User) -> Project:
    project = await db.scalar(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


async def owned_chapter(db: AsyncSession, chapter_id: UUID, user: User) -> Chapter:
    chapter = await db.scalar(
        select(Chapter).join(Project).where(Chapter.id == chapter_id, Project.user_id == user.id)
    )
    if chapter is None:
        raise HTTPException(404, "Chapter not found")
    return chapter


@router.post("/projects", response_model=ProjectOutput, status_code=201)
async def create_project(
    payload: NameInput, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    project = Project(user_id=user.id, name=payload.name)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectOutput])
async def list_projects(db: AsyncSession = Depends(get_db), user: User = Depends(require_user)):
    return (
        await db.scalars(
            select(Project)
            .where(Project.user_id == user.id)
            .order_by(Project.created_at, Project.id)
        )
    ).all()


@router.get("/projects/{project_id}", response_model=ProjectOutput)
async def get_project(
    project_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    return await owned_project(db, project_id, user)


@router.patch("/projects/{project_id}", response_model=ProjectOutput)
async def update_project(
    project_id: UUID,
    payload: NameInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    project = await owned_project(db, project_id, user)
    project.name = payload.name
    await db.commit()
    await db.refresh(project)
    return project


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await db.delete(await owned_project(db, project_id, user))
    await db.commit()
    return {"success": True}


@router.post("/projects/{project_id}/chapters", response_model=ChapterOutput, status_code=201)
async def create_chapter(
    project_id: UUID,
    payload: NameInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    await owned_project(db, project_id, user)
    chapter = Chapter(project_id=project_id, name=payload.name)
    db.add(chapter)
    await db.commit()
    await db.refresh(chapter)
    return chapter


@router.get("/projects/{project_id}/chapters", response_model=list[ChapterOutput])
async def list_chapters(
    project_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_project(db, project_id, user)
    return (
        await db.scalars(
            select(Chapter)
            .where(Chapter.project_id == project_id)
            .order_by(Chapter.created_at, Chapter.id)
        )
    ).all()


@router.get("/chapters/{chapter_id}", response_model=ChapterOutput)
async def get_chapter(
    chapter_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    return await owned_chapter(db, chapter_id, user)


@router.patch("/chapters/{chapter_id}", response_model=ChapterOutput)
async def update_chapter(
    chapter_id: UUID,
    payload: NameInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    chapter = await owned_chapter(db, chapter_id, user)
    chapter.name = payload.name
    await db.commit()
    await db.refresh(chapter)
    return chapter


@router.delete("/chapters/{chapter_id}")
async def delete_chapter(
    chapter_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await db.delete(await owned_chapter(db, chapter_id, user))
    await db.commit()
    return {"success": True}
