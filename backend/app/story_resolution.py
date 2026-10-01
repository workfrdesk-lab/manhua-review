"""Conservative, deterministic reconciliation of validated chunk extractions.

Names alone never establish identity. Only panel/OCR overlap is strong enough
to resolve identity automatically; page-only coincidences require human review.
"""

import re
import unicodedata

from app.story_schemas import (
    ChapterSummaryData,
    StoryStructuredOutput,
    SummaryClaim,
)


def normalize(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "").casefold()).strip()


def sources(item):
    return {
        (e.page_id, e.panel_id, e.ocr_result_id)
        for e in item.evidence
        if e.panel_id or e.ocr_result_id
    }


def union(left, right):
    result = list(left)
    for item in right:
        if item not in result:
            result.append(item)
    return result


def deduplicate_aliases(aliases):
    """Deduplicate only within an already-resolved character identity."""
    result = {}
    for original in aliases:
        alias = original.model_copy(deep=True)
        key = normalize(alias.alias)
        if not key:
            raise ValueError("Alias must not be blank")
        if key in result:
            existing = result[key]
            existing.evidence = union(existing.evidence, alias.evidence)
            existing.confidence = max(existing.confidence, alias.confidence)
            if existing.status != alias.status:
                existing.status = "needs_review"
        else:
            result[key] = alias
    return list(result.values())


class CharacterResolver:
    def __init__(self):
        self.decisions = []

    def resolve(self, candidates, existing=()):
        resolved = [c.model_copy(deep=True) for c in existing]
        refs = {c.temp_id: c.temp_id for c in resolved}
        for candidate in candidates:
            c = candidate.model_copy(deep=True)
            names = {normalize(c.name), *(normalize(a.alias) for a in c.aliases)}
            matches = []
            for other in resolved:
                other_names = {normalize(other.name), *(normalize(a.alias) for a in other.aliases)}
                conflict = any(
                    getattr(c, field)
                    and getattr(other, field)
                    and normalize(getattr(c, field)) != normalize(getattr(other, field))
                    for field in ("description", "gender", "age_group")
                )
                if names & other_names and sources(c) & sources(other) and not conflict:
                    matches.append(other)
            if len(matches) == 1:
                target = matches[0]
                refs[c.temp_id] = target.temp_id
                target.evidence = union(target.evidence, c.evidence)
                target.aliases = deduplicate_aliases([*target.aliases, *c.aliases])
                self.decisions.append(
                    {
                        "source": c.temp_id,
                        "target": target.temp_id,
                        "reason": (
                            "Name or supported alias and shared panel/OCR; no contextual conflict"
                        ),
                        "confidence": min(c.confidence, target.confidence, 0.95),
                    }
                )
            else:
                c.status = "needs_review"
                c.aliases = deduplicate_aliases(c.aliases)
                resolved.append(c)
                refs[c.temp_id] = c.temp_id
        return resolved, refs


class SceneResolver:
    def resolve(self, candidates, character_refs):
        resolved = []
        for candidate in candidates:
            scene = candidate.model_copy(deep=True)
            if scene.start_page > scene.end_page:
                raise ValueError("Invalid scene range")
            scene.character_refs = list(
                dict.fromkeys(character_refs[r] for r in scene.character_refs)
            )
            for event in scene.events:
                event.character_refs = list(
                    dict.fromkeys(character_refs[r] for r in event.character_refs)
                )
            if not scene.evidence:
                scene.status, scene.confidence = "needs_review", min(scene.confidence, 0.3)
            matches = [
                s
                for s in resolved
                if sources(s) & sources(scene)
                and normalize(s.summary) == normalize(scene.summary)
                and normalize(s.location) == normalize(scene.location)
                and max(s.start_page, scene.start_page) <= min(s.end_page, scene.end_page)
            ]
            if len(matches) == 1:
                target = matches[0]
                target.start_page = min(target.start_page, scene.start_page)
                target.end_page = max(target.end_page, scene.end_page)
                target.evidence = union(target.evidence, scene.evidence)
                target.character_refs = union(target.character_refs, scene.character_refs)
                target.events.extend(scene.events)
            else:
                resolved.append(scene)
        return sorted(resolved, key=lambda s: (s.start_page, s.end_page, s.temp_id))


class EventResolver:
    def resolve(self, scenes, panels=()):
        panel_order = {p.id: p.reading_order for p in panels}
        all_events = []
        for scene in scenes:
            resolved = []
            for event in scene.events:
                if not event.evidence:
                    event.status, event.confidence = "needs_review", min(event.confidence, 0.3)
                evidence = sorted(
                    event.evidence,
                    key=lambda e: (
                        e.page_number or scene.start_page,
                        panel_order.get(e.panel_id, 0),
                    ),
                )
                event.page = evidence[0].page_number if evidence else scene.start_page
                event.panel = evidence[0].panel_id if evidence else None
                matches = [
                    e
                    for e in resolved
                    if sources(e) & sources(event)
                    and normalize(e.description) == normalize(event.description)
                    and e.event_type == event.event_type
                    and set(e.character_refs) == set(event.character_refs)
                ]
                if len(matches) == 1:
                    matches[0].evidence = union(matches[0].evidence, event.evidence)
                else:
                    resolved.append(event)
                    all_events.append(event)
            scene.events = resolved
        all_events.sort(key=lambda e: (e.page, panel_order.get(e.panel, 0), e.temp_id))
        for sequence, event in enumerate(all_events, 1):
            event.sequence = sequence
        for scene in scenes:
            scene.events.sort(key=lambda e: e.sequence)
        return scenes


class RelationshipResolver:
    def resolve(self, candidates, character_refs, characters):
        valid = {c.temp_id for c in characters}
        resolved = []
        for candidate in candidates:
            r = candidate.model_copy(deep=True)
            r.source_ref = character_refs.get(r.source_ref)
            r.target_ref = character_refs.get(r.target_ref)
            if r.source_ref not in valid or r.target_ref not in valid:
                raise ValueError("Relationship references an unresolved character")
            if r.source_ref == r.target_ref:
                raise ValueError("Resolution produced a self relationship; review identity")
            matches = [
                x
                for x in resolved
                if (x.source_ref, x.target_ref, x.relationship_type, x.description)
                == (r.source_ref, r.target_ref, r.relationship_type, r.description)
            ]
            if matches:
                matches[0].evidence = union(matches[0].evidence, r.evidence)
            else:
                resolved.append(r)
        return resolved


class StorySynthesizer:
    """Extractive synthesis: every sentence is a resolved event, not a new LLM claim."""

    def synthesize(self, characters, scenes, relationships):
        events = sorted(
            (e for s in scenes for e in s.events if e.evidence and e.status != "rejected"),
            key=lambda e: e.sequence,
        )
        claims = [
            SummaryClaim(
                temp_id=f"claim:{e.temp_id}",
                description=e.description,
                event_refs=[e.temp_id],
                confidence=e.confidence,
                evidence=e.evidence,
                status=e.status,
            )
            for e in events
        ]
        evidence = []
        for claim in claims:
            evidence = union(evidence, claim.evidence)
        return StoryStructuredOutput(
            characters=characters,
            scenes=scenes,
            relationships=relationships,
            summary=" ".join(c.description for c in claims),
            logline=claims[0].description if claims else "",
            main_characters=[c.temp_id for c in characters],
            major_events=[e.temp_id for e in events],
            chapter_summary=ChapterSummaryData(
                claims=claims,
                evidence=evidence,
                confidence=min((c.confidence for c in claims), default=0),
            ),
        )


def resolve_story(outputs, panels=()):
    characters, scenes, relationships = [], [], []
    for index, original in enumerate(outputs):
        output = original.model_copy(deep=True)
        prefix = f"chunk{index}:"
        for character in output.characters:
            character.temp_id = prefix + character.temp_id
        for scene in output.scenes:
            scene.temp_id = prefix + scene.temp_id
            scene.character_refs = [prefix + r for r in scene.character_refs]
            for event in scene.events:
                event.temp_id = prefix + event.temp_id
                event.character_refs = [prefix + r for r in event.character_refs]
        for relation in output.relationships:
            relation.temp_id = prefix + relation.temp_id
            relation.source_ref = prefix + relation.source_ref if relation.source_ref else None
            relation.target_ref = prefix + relation.target_ref if relation.target_ref else None
        characters.extend(output.characters)
        scenes.extend(output.scenes)
        relationships.extend(output.relationships)
    resolver = CharacterResolver()
    characters, refs = resolver.resolve(characters)
    scenes = SceneResolver().resolve(scenes, refs)
    scenes = EventResolver().resolve(scenes, panels)
    relationships = RelationshipResolver().resolve(relationships, refs, characters)
    return StorySynthesizer().synthesize(characters, scenes, relationships), resolver.decisions
