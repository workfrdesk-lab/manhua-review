"""Fail-closed validation against a chapter-scoped source snapshot."""

from app.story_schemas import StoryStructuredOutput


class StoryValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Story output contains invalid source references")


def validate_story(output: StoryStructuredOutput, pages, panels, ocr):
    page_map = {p.id: p for p in pages}
    numbers = {p.page_number: p for p in pages}
    panel_map = {p.id: p for p in panels if p.page_id in page_map}
    ocr_map = {r.id: r for r in ocr if r.panel_id in panel_map}
    errors, warnings = [], []
    refs = {c.temp_id for c in output.characters}
    ids = set()

    def check(item, label):
        if item.temp_id in ids:
            errors.append(f"{label}: duplicate temporary ID")
        ids.add(item.temp_id)
        if not item.evidence:
            errors.append(f"{label}: factual claim requires evidence")
        elif item.confidence < 0.65:
            item.status = "needs_review"
        if item.status not in {"confirmed", "needs_review", "rejected"}:
            errors.append(f"{label}: invalid review state")
        for evidence in item.evidence:
            page = page_map.get(evidence.page_id) if evidence.page_id else None
            if evidence.page_id and page is None:
                errors.append(f"{label}: page is missing or outside chapter")
                continue
            if page is None and evidence.page_number is not None:
                page = numbers.get(evidence.page_number)
            if page is None:
                errors.append(f"{label}: page is missing or outside chapter")
                continue
            if evidence.page_number is not None and evidence.page_number != page.page_number:
                errors.append(f"{label}: page number mismatch")
            panel = panel_map.get(evidence.panel_id) if evidence.panel_id else None
            if evidence.panel_id and (panel is None or panel.page_id != page.id):
                errors.append(f"{label}: panel is missing or belongs to another page")
            record = ocr_map.get(evidence.ocr_result_id) if evidence.ocr_result_id else None
            if evidence.ocr_result_id and (
                record is None or panel is None or record.panel_id != panel.id
            ):
                errors.append(f"{label}: OCR is missing or belongs to another panel")
            if evidence.quote and (record is None or evidence.quote not in record.text):
                errors.append(f"{label}: quote is not present in referenced OCR")
        for ref in getattr(item, "character_refs", []):
            if ref not in refs:
                errors.append(f"{label}: unresolved character reference")

    for character in output.characters:
        check(character, character.temp_id)
        for alias in character.aliases:
            check(alias, alias.temp_id)
        if (
            character.first_appearance_page is not None
            and character.first_appearance_page not in numbers
        ):
            errors.append(f"{character.temp_id}: invalid first appearance")
    for scene in output.scenes:
        check(scene, scene.temp_id)
        if scene.start_page not in numbers or scene.end_page not in numbers:
            errors.append(f"{scene.temp_id}: invalid scene range")
        for event in scene.events:
            check(event, event.temp_id)
            for evidence in event.evidence:
                if (
                    evidence.page_number is not None
                    and not scene.start_page <= evidence.page_number <= scene.end_page
                ):
                    errors.append(f"{event.temp_id}: evidence outside scene range")
    for relation in output.relationships:
        check(relation, relation.temp_id)
        if relation.source_ref not in refs or relation.target_ref not in refs:
            errors.append(f"{relation.temp_id}: unresolved relationship character")
        elif relation.source_ref == relation.target_ref:
            errors.append(f"{relation.temp_id}: self relationship")
    if output.chapter_summary:
        events = {e.temp_id: e for s in output.scenes for e in s.events}
        for claim in output.chapter_summary.claims:
            check(claim, claim.temp_id)
            if any(ref not in events for ref in claim.event_refs):
                errors.append(f"{claim.temp_id}: unresolved summary event")
            elif not any(claim.description == events[ref].description for ref in claim.event_refs):
                errors.append(f"{claim.temp_id}: summary is not grounded in resolved events")
    if errors:
        raise StoryValidationError(errors)
    return warnings
