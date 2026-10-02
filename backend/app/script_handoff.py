"""Canonical, transient handoff. Consumers must recheck before start/publication.

Only an authenticated ownership context may call this service. A successful
snapshot is neither a lease nor a publication fence. Never reuse caller ORM state.
"""

import json
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, text

from app.auth import require_user
from app.models import ScriptVersion, User
from app.projects import owned_chapter, protect_write
from app.script import dependency, validated_candidate

router = APIRouter(prefix="/api/v1", dependencies=[Depends(protect_write)])


@dataclass(frozen=True)
class ApprovedScriptHandoff:
    """Fully materialized canonical JSON DTO; contains no ORM references."""

    content: bytes

    def serialize(self) -> bytes:
        return self.content


class DependencyNotEligible(Exception):
    def __init__(self, reasons):
        self.reasons = tuple(sorted(set(reasons)))
        super().__init__("Script is not eligible for downstream use")

    def representation(self):
        return {
            "success": False,
            "error": {
                "code": "DEPENDENCY_NOT_ELIGIBLE",
                "message": str(self),
                "reasons": list(self.reasons),
            },
        }


async def approved_script_handoff(
    session_factory, user: User, chapter_id: UUID, script_version_id: UUID
) -> ApprovedScriptHandoff:
    """Own the entire read boundary, including ownership and serialization.

    The factory must create fresh sessions, not return a caller's active session.
    Authentication supplies user; ownership is always evaluated in this snapshot.
    """
    async with session_factory() as db:
        async with db.begin():
            if db.bind.dialect.name == "postgresql":
                await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            await owned_chapter(db, chapter_id, user)
            script = await db.scalar(
                select(ScriptVersion).where(
                    ScriptVersion.id == script_version_id, ScriptVersion.chapter_id == chapter_id
                )
            )
            if script is None:
                raise HTTPException(404, "Script not found")
            try:
                candidate, pages = await validated_candidate(
                    db, script, script.data, include_sources=True
                )
            except ValueError:
                identity = dependency(script, source_valid=False)
            else:
                identity = dependency(script)
            if not identity["eligible"]:
                raise DependencyNotEligible(identity["reasons"])
            page_numbers = {page.id: page.page_number for page in pages}
            projected = candidate.model_dump(mode="json", exclude={"status"})
            for segment, original in zip(projected["segments"], candidate.segments, strict=True):
                segment.pop("temp_id")
                segment["evidence"] = [
                    {
                        "scene_ref": evidence.scene_ref,
                        "event_ref": evidence.event_ref,
                        "page_number": page_numbers[evidence.page_id]
                        if evidence.page_id
                        else evidence.page_number,
                        "panel_id": str(evidence.panel_id) if evidence.panel_id else None,
                        "ocr_result_id": str(evidence.ocr_result_id)
                        if evidence.ocr_result_id
                        else None,
                        "quote": evidence.quote,
                    }
                    for evidence in original.evidence
                ]
            return ApprovedScriptHandoff(
                json.dumps(
                    {
                        "schema": "approved-script-handoff-v1",
                        "dependency": identity,
                        "script": projected,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )


@router.get("/chapters/{chapter_id}/scripts/{script_version_id}/handoff")
async def get_handoff(
    chapter_id: UUID, script_version_id: UUID, request: Request, user: User = Depends(require_user)
):
    try:
        dto = await approved_script_handoff(
            request.app.state.db_session_factory, user, chapter_id, script_version_id
        )
    except DependencyNotEligible as exc:
        return Response(
            json.dumps(exc.representation()),
            status_code=409,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    return Response(
        dto.serialize(), media_type="application/json", headers={"Cache-Control": "no-store"}
    )
