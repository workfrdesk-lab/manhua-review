import hashlib
import json
import logging
import time
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.jobs import job_engine
from app.models import (
    ChapterUnderstanding,
    Character,
    CharacterAlias,
    Event,
    EventCharacter,
    OCRResult,
    Page,
    Panel,
    Scene,
    SceneCharacter,
    StoryAnalysisJob,
    StoryAudit,
    StoryRelationship,
    StoryVersion,
    VisualAnalysis,
)
from app.storage import get_storage
from app.story_context import ContextBuilder, ContextChunker
from app.story_providers import ProviderNotConfigured, configured_provider
from app.story_resolution import deduplicate_aliases, normalize, resolve_story
from app.story_validation import StoryValidationError, validate_story

logger = logging.getLogger(__name__)


def story_queue_status(job_id: str) -> str:
    engine = job_engine()
    try:
        with Session(engine) as db:
            job = db.get(StoryAnalysisJob, UUID(job_id))
            if job is None:
                raise ValueError("Story job not found")
            return job.status
    finally:
        engine.dispose()


def cancel_story(job_id: str) -> bool:
    """Only unclaimed work can be cancelled; duplicate deliveries then do nothing."""
    engine = job_engine()
    try:
        with Session(engine) as db:
            result = db.execute(
                update(StoryAnalysisJob)
                .where(StoryAnalysisJob.id == UUID(job_id), StoryAnalysisJob.status == "pending")
                .values(status="cancelled", error="Cancelled before processing")
            )
            db.commit()
            return bool(result.rowcount)
    finally:
        engine.dispose()


def fingerprint(db, chapter_id):
    pages = db.scalars(
        select(Page).where(Page.chapter_id == chapter_id).order_by(Page.page_number)
    ).all()
    panels = db.scalars(select(Panel).join(Page).where(Page.chapter_id == chapter_id)).all()
    panel_ids = [panel.id for panel in panels]
    ocr = (
        db.scalars(select(OCRResult).where(OCRResult.panel_id.in_(panel_ids))).all()
        if panel_ids
        else []
    )
    visual = (
        db.scalars(select(VisualAnalysis).where(VisualAnalysis.panel_id.in_(panel_ids))).all()
        if panel_ids
        else []
    )
    value = []
    for records in (pages, panels, ocr, visual):
        value.append(
            [
                {
                    c.name: getattr(item, c.name)
                    for c in item.__table__.columns
                    if c.name not in {"created_at", "updated_at"}
                }
                for item in sorted(records, key=lambda r: str(r.id))
            ]
        )
    settings = get_settings()
    storage = get_storage()
    value.append({str(page.id): storage.content_hash(page.storage_key) for page in pages})
    value.append(
        {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "contract": "story-4a-v2",
            "max_chars": 18000,
            "max_pages": 8,
            "overlap": 1,
        }
    )
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest(), pages


def process_story(job_id: str) -> None:
    engine = job_engine()
    started = time.monotonic()
    try:
        with Session(engine) as db:
            claimed = db.execute(
                update(StoryAnalysisJob)
                .where(StoryAnalysisJob.id == UUID(job_id), StoryAnalysisJob.status == "pending")
                .values(status="analyzing")
            )
            db.commit()
            if not claimed.rowcount:
                return
            job = db.get(StoryAnalysisJob, UUID(job_id))
            try:
                provider = configured_provider()
                job.provider, job.model = provider.name, provider.model
                source_fingerprint, pages = fingerprint(db, job.chapter_id)
                if db.scalar(
                    select(StoryVersion).where(
                        StoryVersion.chapter_id == job.chapter_id,
                        StoryVersion.fingerprint == source_fingerprint,
                    )
                ):
                    job.status = "completed"
                    db.commit()
                    return
                panels = db.scalars(
                    select(Panel).join(Page).where(Page.chapter_id == job.chapter_id)
                ).all()
                panel_ids = [p.id for p in panels]
                ocr = (
                    {
                        r.panel_id: {**r.data_json, "text": r.text, "id": str(r.id)}
                        for r in db.scalars(
                            select(OCRResult).where(OCRResult.panel_id.in_(panel_ids))
                        ).all()
                    }
                    if panel_ids
                    else {}
                )
                visual = (
                    {
                        r.panel_id: r.data_json
                        for r in db.scalars(
                            select(VisualAnalysis).where(VisualAnalysis.panel_id.in_(panel_ids))
                        ).all()
                    }
                    if panel_ids
                    else {}
                )
                by_page = {}
                for panel in panels:
                    by_page.setdefault(panel.page_id, []).append(panel)
                context = ContextBuilder().build(pages, by_page, ocr, visual)
                chunks = ContextChunker().chunk(context)
                job.pages_processed, job.chunks = len(pages), len(chunks)
                outputs = []
                source_ocr = db.scalars(
                    select(OCRResult).where(OCRResult.panel_id.in_(panel_ids))
                ).all()
                for chunk in chunks:
                    response = provider.generate_structured(chunk.text)
                    warnings = validate_story(
                        response.output,
                        [p for p in pages if p.page_number in chunk.pages],
                        panels,
                        source_ocr,
                    )
                    job.validation_warnings = [*job.validation_warnings, *warnings]
                    outputs.append(response.output)
                    job.input_tokens += response.input_tokens
                    job.output_tokens += response.output_tokens
                output, decisions = resolve_story(outputs, panels)
                validate_story(output, pages, panels, source_ocr)
                # Abort stale work instead of publishing a graph against changed sources.
                db.flush()
                db.execute(
                    update(StoryAnalysisJob)
                    .where(StoryAnalysisJob.id == UUID(job_id))
                    .values(updated_at=StoryAnalysisJob.updated_at)
                )
                db.expire_all()
                if fingerprint(db, job.chapter_id)[0] != source_fingerprint:
                    raise ValueError("Source changed during story analysis")
                version = StoryVersion(
                    chapter_id=job.chapter_id,
                    fingerprint=source_fingerprint,
                    data=output.model_dump(mode="json"),
                    decisions=decisions,
                )
                db.add(version)
                db.flush()
                persist(db, job.chapter_id, output, version.id)
                job.source_fingerprint, job.status = source_fingerprint, "completed"
                db.commit()
            except StoryValidationError as exc:
                db.rollback()
                job = db.get(StoryAnalysisJob, UUID(job_id))
                job.status, job.error = "failed", str(exc)
                job.validation_errors = exc.errors
                db.commit()
            except ProviderNotConfigured as exc:
                db.rollback()
                job = db.get(StoryAnalysisJob, UUID(job_id))
                job.status, job.error = "provider_not_configured", str(exc)
                db.commit()
            except Exception:
                db.rollback()
                job = db.get(StoryAnalysisJob, UUID(job_id))
                job.status, job.error = "failed", "Story analysis failed; see server logs"
                db.commit()
                logger.exception("story_analysis_failed")
    finally:
        if "job" in locals() and job:
            with Session(engine) as db:
                current = db.get(StoryAnalysisJob, UUID(job_id))
                if current:
                    current.duration = round(time.monotonic() - started, 3)
                    db.commit()
        engine.dispose()


def merge_outputs(outputs):
    return resolve_story(outputs)[0]


def record_events(db, scene_id):
    return db.scalars(select(Event).where(Event.scene_id == scene_id)).all()


def persist(db, chapter_id, output, version_id=None):
    from app.story_audit import audit_reconciliation, graph_snapshot

    before = graph_snapshot(db, chapter_id)
    previous_ids = set(db.scalars(select(StoryAudit.id).where(StoryAudit.chapter_id == chapter_id)))
    _persist(db, chapter_id, output, version_id)
    after = graph_snapshot(db, chapter_id)
    audit_reconciliation(db, chapter_id, before, after, version_id, previous_ids)


def _persist(db, chapter_id, output, version_id=None):
    def canonical_claims(claims):
        """Keep the embedded reviewable claims on the same canonical contract."""
        normalized = []
        for claim in claims or []:
            value = dict(claim)
            if value.get("status") not in {"needs_review", "confirmed", "rejected"}:
                value["status"] = "needs_review"
            normalized.append(value)
        return normalized

    protected = []
    for model in (Character, Scene, StoryRelationship, ChapterUnderstanding):
        protected.extend(
            db.scalars(
                select(model).where(
                    model.chapter_id == chapter_id, model.provenance != "ai_generated"
                )
            )
        )
    protected.extend(
        db.scalars(
            select(Event)
            .join(Scene)
            .where(Scene.chapter_id == chapter_id, Event.provenance != "ai_generated")
        )
    )
    existing_graph = any(
        db.scalar(select(model.id).where(model.chapter_id == chapter_id)) is not None
        for model in (Character, Scene, StoryRelationship, ChapterUnderstanding)
    )
    if protected or existing_graph:

        def evidence_keys(value):
            return {
                (
                    e.get("page_id"),
                    e.get("page_number"),
                    e.get("panel_id"),
                    e.get("ocr_result_id"),
                )
                for e in value or []
            }

        def reconcile(record, item, fields):
            edited = set(record.edited_fields or [])
            if record.status in {"confirmed", "rejected"} and (
                record.provenance != "ai_generated" or edited
            ):
                edited.update(fields)
            conflicts = list(record.review_conflicts or [])
            for field in fields:
                proposed = getattr(item, field)
                if field == "display_name":
                    proposed = proposed or item.name
                current = getattr(record, field)
                if field in edited and proposed != current:
                    conflict = {
                        "version_id": str(version_id),
                        "field": field,
                        "current": current,
                        "proposed": proposed,
                    }
                    if conflict in conflicts:
                        continue
                    conflicts.append(conflict)
                    db.add(
                        StoryAudit(
                            chapter_id=chapter_id,
                            action="ai_conflict",
                            data={
                                "entity": str(record.id),
                                "field": field,
                                "version_id": str(version_id),
                                "before": {field: current},
                                "after": {field: current},
                                "proposal": {field: proposed},
                            },
                        )
                    )
                elif field not in edited and current != proposed:
                    setattr(record, field, proposed)
                    db.add(
                        StoryAudit(
                            chapter_id=chapter_id,
                            action="ai_reconcile",
                            data={
                                "entity": str(record.id),
                                "field": field,
                                "version_id": str(version_id),
                                "before": {field: current},
                                "after": {field: proposed},
                            },
                        )
                    )
            record.review_conflicts = conflicts
            if hasattr(item, "evidence"):
                from app.story_resolution import union

                record.evidence = union(
                    record.evidence or [], [e.model_dump(mode="json") for e in item.evidence]
                )

        characters = db.scalars(select(Character).where(Character.chapter_id == chapter_id)).all()
        matched_characters = set()
        resolved_character_records = {}
        for item in output.characters:
            candidate_keys = evidence_keys([e.model_dump(mode="json") for e in item.evidence])
            # Co-occurrence is not identity: several people can share a panel.
            # Prefer a unique name match; evidence-only fallback supports renamed
            # characters only when exactly one unused record is a candidate.
            available = [r for r in characters if r.id not in matched_characters]
            matches = [r for r in available if normalize(r.name) == normalize(item.name)]
            if not matches and candidate_keys:
                matches = [r for r in available if candidate_keys & evidence_keys(r.evidence)]
                matches = [r for r in matches if "name" in (r.edited_fields or [])]
            record = matches[0] if len(matches) == 1 else None
            if record:
                matched_characters.add(record.id)
                resolved_character_records[item.temp_id] = record
                reconcile(
                    record,
                    item,
                    ("name", "display_name", "description", "gender", "age_group", "importance"),
                )
                record.evidence = list(
                    {
                        str(e): e
                        for e in [
                            *record.evidence,
                            *[x.model_dump(mode="json") for x in item.evidence],
                        ]
                    }.values()
                )
                for alias in deduplicate_aliases(item.aliases):
                    normalized = normalize(alias.alias)
                    existing_alias = db.scalar(
                        select(CharacterAlias).where(
                            CharacterAlias.character_id == record.id,
                            CharacterAlias.normalized_alias == normalized,
                        )
                    )
                    if existing_alias:
                        existing_alias.evidence = list(
                            {
                                str(e): e
                                for e in [
                                    *existing_alias.evidence,
                                    *[x.model_dump(mode="json") for x in alias.evidence],
                                ]
                            }.values()
                        )
                        existing_alias.confidence = max(existing_alias.confidence, alias.confidence)
                    else:
                        db.add(
                            CharacterAlias(
                                character_id=record.id,
                                alias=alias.alias,
                                normalized_alias=normalized,
                                confidence=alias.confidence,
                                evidence=[e.model_dump(mode="json") for e in alias.evidence],
                            )
                        )
                record.review_conflicts = record.review_conflicts
            else:
                # A new AI identity is additive. It never replaces an omitted
                # human record and remains reviewable until confirmed.
                record = Character(
                    chapter_id=chapter_id,
                    name=item.name,
                    display_name=item.display_name or item.name,
                    description=item.description,
                    gender=item.gender,
                    age_group=item.age_group,
                    importance=item.importance,
                    importance_reason=item.importance_reason,
                    first_appearance_page=item.first_appearance_page,
                    confidence=item.confidence,
                    status=item.status,
                    evidence=[e.model_dump(mode="json") for e in item.evidence],
                )
                db.add(record)
                db.flush()
                resolved_character_records[item.temp_id] = record
                characters.append(record)
                matched_characters.add(record.id)
                for alias in deduplicate_aliases(item.aliases):
                    db.add(
                        CharacterAlias(
                            character_id=record.id,
                            alias=alias.alias,
                            normalized_alias=normalize(alias.alias),
                            confidence=alias.confidence,
                            evidence=[e.model_dump(mode="json") for e in alias.evidence],
                        )
                    )

        # Omission is not deletion. Keep reviewed/edited records and surface
        # the omission for a human rather than silently changing their state.
        for record in characters:
            if record.id not in matched_characters and record.provenance != "ai_generated":
                conflict = {
                    "version_id": str(version_id),
                    "kind": "ai_omission",
                    "entity": str(record.id),
                }
                if conflict not in (record.review_conflicts or []):
                    record.review_conflicts = [*(record.review_conflicts or []), conflict]
                    db.add(
                        StoryAudit(
                            chapter_id=chapter_id,
                            action="ai_conflict",
                            data={
                                "entity": str(record.id),
                                "kind": "ai_omission",
                                "before": {"status": record.status},
                                "after": {"status": record.status},
                            },
                        )
                    )
        scenes = db.scalars(select(Scene).where(Scene.chapter_id == chapter_id)).all()
        for item in output.scenes:
            keys = evidence_keys([e.model_dump(mode="json") for e in item.evidence])
            record = next((r for r in scenes if keys & evidence_keys(r.evidence)), None)
            if record is None:
                next_index = max((scene.scene_index for scene in scenes), default=0) + 1
                record = Scene(
                    chapter_id=chapter_id,
                    scene_index=next_index,
                    title=item.title,
                    summary=item.summary,
                    start_page=item.start_page,
                    end_page=item.end_page,
                    importance=item.importance,
                    importance_reason=item.importance_reason,
                    confidence=item.confidence,
                    location=item.location,
                    status=item.status,
                    evidence=[e.model_dump(mode="json") for e in item.evidence],
                )
                db.add(record)
                db.flush()
                scenes.append(record)
                db.add(
                    StoryAudit(
                        chapter_id=chapter_id,
                        action="ai_reconcile",
                        data={
                            "entity": str(record.id),
                            "entity_type": "scene",
                            "version_id": str(version_id),
                            "before": None,
                            "after": {"id": str(record.id), "title": record.title},
                        },
                    )
                )
            if record:
                reconcile(
                    record,
                    item,
                    ("title", "summary", "start_page", "end_page", "importance", "location"),
                )
                for event in item.events:
                    event_keys = evidence_keys([e.model_dump(mode="json") for e in event.evidence])
                    existing = next(
                        (
                            e
                            for e in record_events(db, record.id)
                            if event_keys & evidence_keys(e.evidence)
                        ),
                        None,
                    )
                    if existing is None:
                        existing = Event(
                            scene_id=record.id,
                            event_index=max(
                                (e.event_index for e in record_events(db, record.id)), default=0
                            )
                            + 1,
                            description=event.description,
                            event_type=event.event_type,
                            importance=event.importance,
                            importance_reason=event.importance_reason,
                            confidence=event.confidence,
                            status=event.status,
                            evidence=[e.model_dump(mode="json") for e in event.evidence],
                            page_id=event.evidence[0].page_id,
                            panel_id=event.panel,
                        )
                        db.add(existing)
                        db.flush()
                    if existing:
                        reconcile(existing, event, ("description", "event_type", "importance"))
                        for ref in event.character_refs:
                            character = resolved_character_records.get(ref)
                            if character and not db.get(
                                EventCharacter, (existing.id, character.id)
                            ):
                                db.add(
                                    EventCharacter(event_id=existing.id, character_id=character.id)
                                )
                for ref in item.character_refs:
                    character = resolved_character_records.get(ref)
                    if character and not db.get(SceneCharacter, (record.id, character.id)):
                        db.add(SceneCharacter(scene_id=record.id, character_id=character.id))
        # Relationship policy is the same as every other story entity: match the
        # resolved edge, preserve edited fields, and keep missing AI edges for review.
        relationships = db.scalars(
            select(StoryRelationship).where(StoryRelationship.chapter_id == chapter_id)
        ).all()
        matched_relationships = set()
        for item in output.relationships:
            source_record = resolved_character_records.get(item.source_ref)
            target_record = resolved_character_records.get(item.target_ref)
            if source_record and target_record and source_record.id == target_record.id:
                continue
            record = next(
                (
                    relation
                    for relation in relationships
                    if source_record
                    and target_record
                    and relation.source_character_id == source_record.id
                    and relation.target_character_id == target_record.id
                    and relation.relationship_type == item.relationship_type
                ),
                None,
            )
            if record is None and source_record and target_record:
                same_endpoints = [
                    relation
                    for relation in relationships
                    if relation.source_character_id == source_record.id
                    and relation.target_character_id == target_record.id
                    and relation.id not in matched_relationships
                ]
                if len(same_endpoints) == 1:
                    record = same_endpoints[0]
                    if record.relationship_type != item.relationship_type:
                        if (
                            record.provenance != "ai_generated"
                            or record.edited_fields
                            or record.status in {"confirmed", "rejected"}
                        ):
                            conflict = {
                                "version_id": str(version_id),
                                "kind": "ai_relationship_type_change",
                                "current": record.relationship_type,
                                "proposed": item.relationship_type,
                            }
                            if conflict not in (record.review_conflicts or []):
                                record.review_conflicts = [
                                    *(record.review_conflicts or []),
                                    conflict,
                                ]
                        else:
                            record.relationship_type = item.relationship_type
            if record:
                matched_relationships.add(record.id)
                reconcile(record, item, ("description", "confidence"))
                record.evidence = list(
                    {
                        str(e): e
                        for e in [
                            *record.evidence,
                            *[x.model_dump(mode="json") for x in item.evidence],
                        ]
                    }.values()
                )
            elif source_record and target_record:
                record = StoryRelationship(
                    chapter_id=chapter_id,
                    source_character_id=source_record.id,
                    target_character_id=target_record.id,
                    relationship_type=item.relationship_type,
                    description=item.description,
                    confidence=item.confidence,
                    status=item.status,
                    evidence=[e.model_dump(mode="json") for e in item.evidence],
                )
                db.add(record)
                db.flush()
                matched_relationships.add(record.id)
                relationships.append(record)
                db.add(
                    StoryAudit(
                        chapter_id=chapter_id,
                        action="ai_reconcile",
                        data={
                            "entity": str(record.id),
                            "entity_type": "relationship",
                            "version_id": str(version_id),
                            "before": None,
                            "after": {
                                "source": str(source_record.id),
                                "target": str(target_record.id),
                                "relationship_type": record.relationship_type,
                            },
                        },
                    )
                )
        for record in relationships:
            if record.id not in matched_relationships and record.provenance != "ai_generated":
                conflict = {
                    "version_id": str(version_id),
                    "kind": "ai_omission",
                    "entity": str(record.id),
                }
                if conflict not in (record.review_conflicts or []):
                    record.review_conflicts = [*(record.review_conflicts or []), conflict]
        understanding = db.scalar(
            select(ChapterUnderstanding).where(ChapterUnderstanding.chapter_id == chapter_id)
        )
        if understanding is None and any((output.logline, output.summary, output.chapter_summary)):
            understanding = ChapterUnderstanding(
                chapter_id=chapter_id,
                logline=output.logline,
                summary=output.summary,
                main_characters=output.main_characters,
                major_events=output.major_events,
                conflicts=output.conflicts,
                revelations=output.revelations,
                ending_state=output.ending_state,
                claims=(
                    [claim.model_dump(mode="json") for claim in output.chapter_summary.claims]
                    if output.chapter_summary
                    else []
                ),
                evidence=(
                    [item.model_dump(mode="json") for item in output.chapter_summary.evidence]
                    if output.chapter_summary
                    else []
                ),
                confidence=output.chapter_summary.confidence if output.chapter_summary else 0,
            )
            db.add(understanding)
            db.flush()
        if understanding:
            understanding.claims = canonical_claims(understanding.claims)
            reconcile(
                understanding,
                output,
                (
                    "logline",
                    "summary",
                    "main_characters",
                    "major_events",
                    "conflicts",
                    "revelations",
                    "ending_state",
                ),
            )
            understanding.evidence = list(
                {
                    str(e): e
                    for e in [
                        *understanding.evidence,
                        *(
                            [x.model_dump(mode="json") for x in output.chapter_summary.evidence]
                            if output.chapter_summary
                            else []
                        ),
                    ]
                }.values()
            )
            if (
                output.chapter_summary
                and "claims" not in (understanding.edited_fields or [])
                and understanding.status not in {"confirmed", "rejected"}
                and not any(
                    c.get("status") in {"confirmed", "rejected"}
                    and understanding.provenance != "ai_generated"
                    for c in understanding.claims
                )
            ):
                understanding.claims = canonical_claims(
                    [claim.model_dump(mode="json") for claim in output.chapter_summary.claims]
                )
            elif output.chapter_summary:
                proposed_claims = canonical_claims(
                    [claim.model_dump(mode="json") for claim in output.chapter_summary.claims]
                )
                if proposed_claims != understanding.claims:
                    conflict = {
                        "version_id": str(version_id),
                        "field": "claims",
                        "current": understanding.claims,
                        "proposed": proposed_claims,
                    }
                    if conflict not in (understanding.review_conflicts or []):
                        understanding.review_conflicts = [
                            *(understanding.review_conflicts or []),
                            conflict,
                        ]
                        db.add(
                            StoryAudit(
                                chapter_id=chapter_id,
                                action="ai_conflict",
                                data={
                                    "entity": str(understanding.id),
                                    "field": "claims",
                                    "version_id": str(version_id),
                                    "before": {"claims": understanding.claims},
                                    "after": {"claims": understanding.claims},
                                    "proposal": {"claims": proposed_claims},
                                },
                            )
                        )
        for record in protected:
            record.review_conflicts = record.review_conflicts or []
        return
    db.query(EventCharacter).filter(
        EventCharacter.event_id.in_(
            select(Event.id).join(Scene).where(Scene.chapter_id == chapter_id)
        )
    ).delete(synchronize_session=False)
    db.query(SceneCharacter).filter(
        SceneCharacter.scene_id.in_(select(Scene.id).where(Scene.chapter_id == chapter_id))
    ).delete(synchronize_session=False)
    db.query(Event).filter(
        Event.scene_id.in_(select(Scene.id).where(Scene.chapter_id == chapter_id))
    ).delete(synchronize_session=False)
    db.query(StoryRelationship).filter(StoryRelationship.chapter_id == chapter_id).delete()
    db.query(CharacterAlias).filter(
        CharacterAlias.character_id.in_(
            select(Character.id).where(Character.chapter_id == chapter_id)
        )
    ).delete(synchronize_session=False)
    db.query(Scene).filter(Scene.chapter_id == chapter_id).delete()
    db.query(Character).filter(Character.chapter_id == chapter_id).delete()
    db.query(ChapterUnderstanding).filter(ChapterUnderstanding.chapter_id == chapter_id).delete()
    db.flush()
    chars = {}
    for item in output.characters:
        record = Character(
            chapter_id=chapter_id,
            name=item.name,
            display_name=item.display_name or item.name,
            description=item.description,
            gender=item.gender,
            age_group=item.age_group,
            importance=item.importance,
            importance_reason=item.importance_reason,
            first_appearance_page=item.first_appearance_page,
            confidence=item.confidence,
            status=item.status,
            evidence=[e.model_dump(mode="json") for e in item.evidence],
        )
        db.add(record)
        db.flush()
        chars[item.temp_id] = record
        for alias in deduplicate_aliases(item.aliases):
            db.add(
                CharacterAlias(
                    character_id=record.id,
                    alias=alias.alias,
                    normalized_alias=normalize(alias.alias),
                    confidence=alias.confidence,
                    evidence=[e.model_dump(mode="json") for e in alias.evidence],
                )
            )
    for index, item in enumerate(output.scenes, 1):
        scene = Scene(
            chapter_id=chapter_id,
            scene_index=index,
            title=item.title,
            summary=item.summary,
            start_page=item.start_page,
            end_page=item.end_page,
            importance=item.importance,
            importance_reason=item.importance_reason,
            confidence=item.confidence,
            status=item.status,
            location=item.location,
            evidence=[e.model_dump(mode="json") for e in item.evidence],
        )
        db.add(scene)
        db.flush()
        for ref in item.character_refs:
            db.add(SceneCharacter(scene_id=scene.id, character_id=chars[ref].id))
        for event_index, event in enumerate(item.events, 1):
            record = Event(
                scene_id=scene.id,
                event_index=event.sequence or event_index,
                description=event.description,
                event_type=event.event_type,
                importance=event.importance,
                importance_reason=event.importance_reason,
                confidence=event.confidence,
                status=event.status,
                evidence=[e.model_dump(mode="json") for e in event.evidence],
                page_id=event.evidence[0].page_id if event.evidence else None,
                panel_id=event.panel,
            )
            db.add(record)
            db.flush()
            for ref in event.character_refs:
                db.add(EventCharacter(event_id=record.id, character_id=chars[ref].id))
    for relation in output.relationships:
        source, target = chars.get(relation.source_ref), chars.get(relation.target_ref)
        if not source or not target:
            raise ValueError("Unresolved relationship reference")
        if source and target:
            db.add(
                StoryRelationship(
                    chapter_id=chapter_id,
                    source_character_id=source.id,
                    target_character_id=target.id,
                    relationship_type=relation.relationship_type,
                    description=relation.description,
                    confidence=relation.confidence,
                    status=relation.status,
                    evidence=[e.model_dump(mode="json") for e in relation.evidence],
                )
            )
    db.add(
        ChapterUnderstanding(
            chapter_id=chapter_id,
            logline=output.logline,
            summary=output.summary,
            main_characters=output.main_characters,
            major_events=output.major_events,
            conflicts=output.conflicts,
            revelations=output.revelations,
            ending_state=output.ending_state,
            claims=[c.model_dump(mode="json") for c in output.chapter_summary.claims]
            if output.chapter_summary
            else [],
            evidence=[e.model_dump(mode="json") for e in output.chapter_summary.evidence]
            if output.chapter_summary
            else [],
            confidence=output.chapter_summary.confidence if output.chapter_summary else 0,
        )
    )
