"""Fail-closed structural grounding against an exact immutable StoryVersion.

Reference validation is not semantic entailment: a human must still review paraphrases.
No live Story graph is used to substitute facts from a newer version.
"""

from app.script_schemas import ScriptProfile, ScriptStructuredOutput
from app.story_review import ReviewState
from app.story_schemas import StoryStructuredOutput
from app.story_validation import validate_story


class ScriptValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Script output failed grounding validation")


def validate_script(
    output: ScriptStructuredOutput,
    story: StoryStructuredOutput,
    pages,
    panels,
    ocr,
    profile: ScriptProfile,
    *,
    generated: bool = True,
):
    # The existing validator normalizes low-confidence states; never mutate our snapshot.
    validate_story(story.model_copy(deep=True), pages, panels, ocr)
    numbers = {p.page_number: p for p in pages}
    page_map = {p.id: p for p in pages}
    panel_map = {p.id: p for p in panels if p.page_id in page_map}
    ocr_map = {r.id: r for r in ocr if r.panel_id in panel_map}
    scenes = {s.temp_id: s for s in story.scenes}
    events = {e.temp_id: (s, e) for s in story.scenes for e in s.events}
    characters = {c.temp_id: c for c in story.characters}
    errors = []
    warnings = []

    def error(path, code):
        errors.append({"path": path, "code": code})

    if generated and output.status != ReviewState.NEEDS_REVIEW:
        error("status", "human_review_required")
    for field in ("hook", "intro", "outro"):
        value = getattr(output, field)
        if value and value not in {s.narration_text for s in output.segments}:
            error(field, "front_matter_requires_grounded_segment")
    previous_page = 0
    for index, segment in enumerate(output.segments):
        path = f"segments.{index}"
        if generated and segment.status != ReviewState.NEEDS_REVIEW:
            error(path, "human_review_required")
        if segment.start_page not in numbers or segment.end_page not in numbers:
            error(path, "invalid_page_range")
        if segment.start_page < previous_page:
            error(path, "source_order_violation")
        previous_page = segment.start_page
        scene = scenes.get(segment.scene_ref)
        if scene is None or scene.status == ReviewState.REJECTED:
            error(path, "invalid_scene")
        elif not scene.start_page <= segment.start_page <= segment.end_page <= scene.end_page:
            error(path, "outside_scene_range")
        for ref in segment.event_refs:
            pair = events.get(ref)
            if not pair or pair[0].temp_id != segment.scene_ref:
                error(path, "invalid_event")
            elif pair[1].status == ReviewState.REJECTED:
                error(path, "rejected_event")
        if segment.speaker_ref:
            character = characters.get(segment.speaker_ref)
            if character is None or character.status == ReviewState.REJECTED:
                error(path, "invalid_speaker")
            # Story Understanding does not infer OCR speaker attribution. Fail closed.
            error(path, "source_speaker_attribution_unavailable")
        matched_dialogue = False
        covered = set()
        confidence_limits = []
        for ev_index, evidence in enumerate(segment.evidence):
            ev_path = f"{path}.evidence.{ev_index}"
            page = (
                page_map.get(evidence.page_id)
                if evidence.page_id
                else numbers.get(evidence.page_number)
            )
            if not page:
                error(ev_path, "invalid_page")
                continue
            if evidence.page_number is not None and evidence.page_number != page.page_number:
                error(ev_path, "page_number_mismatch")
            if not segment.start_page <= page.page_number <= segment.end_page:
                error(ev_path, "evidence_outside_range")
            panel = panel_map.get(evidence.panel_id)
            if evidence.panel_id and (panel is None or panel.page_id != page.id):
                error(ev_path, "invalid_panel")
            record = ocr_map.get(evidence.ocr_result_id)
            if evidence.ocr_result_id and (
                record is None or panel is None or record.panel_id != panel.id
            ):
                error(ev_path, "invalid_ocr")
            if evidence.quote and (record is None or evidence.quote not in record.text):
                error(ev_path, "invalid_quote")
            pair = events.get(evidence.event_ref)
            if (
                pair is None
                or evidence.scene_ref != segment.scene_ref
                or pair[0].temp_id != evidence.scene_ref
                or evidence.event_ref not in segment.event_refs
            ):
                error(ev_path, "invalid_story_reference")
                continue
            event = pair[1]
            confidence_limits.append(event.confidence)
            if evidence.character_ref and (
                evidence.character_ref not in characters
                or evidence.character_ref not in event.character_refs
                or characters[evidence.character_ref].status == ReviewState.REJECTED
            ):
                error(ev_path, "invalid_character")

            def same_source(source):
                source_page = (
                    page_map.get(source.page_id)
                    if source.page_id
                    else numbers.get(source.page_number)
                )
                return (
                    source_page is not None
                    and source_page.id == page.id
                    and source.panel_id == evidence.panel_id
                    and source.ocr_result_id == evidence.ocr_result_id
                )

            if not any(same_source(source) for source in event.evidence):
                error(ev_path, "source_not_in_selected_story_event")
            else:
                covered.add(evidence.event_ref)
            if segment.source_dialogue and record and panel and record.panel_id == panel.id:
                if evidence.quote == segment.dialogue_text and evidence.quote in record.text:
                    matched_dialogue = True
                    confidence_limits.append(record.confidence)
        if set(segment.event_refs) != covered:
            error(path, "event_requires_evidence")
        if segment.source_dialogue and (
            not profile.preserve_source_dialogue or not matched_dialogue
        ):
            error(path, "source_dialogue_requires_exact_ocr_quote")
        if confidence_limits and segment.confidence > min(confidence_limits):
            error(path, "confidence_exceeds_source")
        if confidence_limits and min(confidence_limits) < 0.65:
            warnings.append({"path": path, "code": "low_confidence_source"})
    if errors:
        raise ScriptValidationError(errors)
    return warnings
