from uuid import uuid4

import pytest

from app.story_resolution import (
    CharacterResolver,
    EventResolver,
    RelationshipResolver,
    SceneResolver,
)
from app.story_schemas import (
    AliasData,
    ChapterSummaryData,
    CharacterData,
    EventData,
    EvidenceRef,
    RelationshipData,
    SceneData,
    StoryStructuredOutput,
    SummaryClaim,
)


def evidence(page=1, panel=None):
    return [EvidenceRef(page_number=page, panel_id=panel or uuid4(), reason="fixture")]


def character(temp_id, name, page=1, panel=None, description=None, aliases=None):
    return CharacterData(
        temp_id=temp_id,
        name=name,
        description=description,
        importance=0.5,
        confidence=0.9,
        aliases=aliases or [],
        evidence=evidence(page, panel),
    )


def test_empty_story_entities_are_rejected():
    with pytest.raises(ValueError):
        StoryStructuredOutput.model_validate({})
    for field in ("characters", "scenes", "relationships"):
        with pytest.raises(ValueError):
            StoryStructuredOutput.model_validate({field: [{}]})
    for model in (AliasData, ChapterSummaryData, SummaryClaim, EventData):
        with pytest.raises(ValueError):
            model.model_validate({})


def test_supported_alias_and_ambiguous_identity():
    panel = uuid4()
    first = character("one", "Mira", panel=panel)
    second = character(
        "two",
        "Captain",
        panel=panel,
        aliases=[
            AliasData(temp_id="alias", alias="Mira", confidence=0.9, evidence=evidence(1, panel))
        ],
    )
    resolver = CharacterResolver()
    resolved, refs = resolver.resolve([first, second])
    assert len(resolved) == 1 and refs["two"] == "one"
    assert resolver.decisions[0]["reason"] and resolver.decisions[0]["confidence"] > 0
    ambiguous = character("three", "Mira")
    resolved, _ = resolver.resolve([first, ambiguous])
    assert len(resolved) == 2
    assert all(c.status == "needs_review" for c in resolved)


def test_relationship_requires_resolved_character():
    one, two = character("a", "Mira"), character("b", "Jin")
    relation = RelationshipData(
        temp_id="r",
        source="Mira",
        target="Jin",
        source_ref="a",
        target_ref="b",
        relationship_type="ally",
        confidence=0.8,
        evidence=evidence(),
    )
    assert len(RelationshipResolver().resolve([relation], {"a": "a", "b": "b"}, [one, two])) == 1
    with pytest.raises(ValueError):
        RelationshipResolver().resolve([relation], {"a": "a"}, [one])


def test_missing_scene_evidence_is_rejected():
    with pytest.raises(ValueError):
        SceneData(
            temp_id="s",
            title="Unknown",
            summary="Unknown",
            start_page=1,
            end_page=1,
            importance=0.5,
            confidence=0.9,
            status="confirmed",
        )


def test_exact_character_across_chunks_merges_with_reason():
    panel = uuid4()
    first = character("one", "Mira", panel=panel)
    second = character("two", "Mira", panel=panel)
    resolved, refs = CharacterResolver().resolve([first, second])
    assert len(resolved) == 1
    assert refs["two"] == "one"


def test_similar_names_different_contexts_do_not_merge():
    first = character("one", "Jin", description="a teacher")
    second = character("two", "Jin Woo", page=2, description="a soldier")
    resolved, _ = CharacterResolver().resolve([first, second])
    assert len(resolved) == 2
    assert all(item.status == "needs_review" for item in resolved)


def test_scene_boundary_merges_only_matching_evidence_and_context():
    panel = uuid4()
    shared = evidence(2, panel)
    first = SceneData(
        temp_id="a",
        title="Gate",
        summary="They wait",
        start_page=1,
        end_page=2,
        importance=0.4,
        confidence=0.8,
        evidence=shared,
    )
    second = SceneData(
        temp_id="b",
        title="Gate",
        summary="They wait",
        start_page=2,
        end_page=3,
        importance=0.4,
        confidence=0.8,
        evidence=shared,
    )
    assert len(SceneResolver().resolve([first, second], {})) == 1


def test_adjacent_scenes_and_invalid_range_are_not_silently_merged():
    one = SceneData(
        temp_id="a",
        title="A",
        summary="first",
        start_page=1,
        end_page=1,
        importance=0.4,
        confidence=0.8,
        evidence=evidence(1),
    )
    two = SceneData(
        temp_id="b",
        title="B",
        summary="second",
        start_page=2,
        end_page=2,
        importance=0.4,
        confidence=0.8,
        evidence=evidence(2),
    )
    assert len(SceneResolver().resolve([one, two], {})) == 2
    with pytest.raises(ValueError):
        SceneResolver().resolve([one.model_copy(update={"start_page": 3})], {})


def test_events_are_ordered_by_source_page_and_panel_not_input_order():
    early_panel, late_panel = uuid4(), uuid4()
    early = EventData(
        temp_id="late-input",
        description="first",
        importance=0.5,
        confidence=0.8,
        evidence=evidence(1, early_panel),
    )
    late = EventData(
        temp_id="early-input",
        description="second",
        importance=0.5,
        confidence=0.8,
        evidence=evidence(2, late_panel),
    )
    scene = SceneData(
        temp_id="s",
        title="Scene",
        summary="Summary",
        start_page=1,
        end_page=2,
        importance=0.5,
        confidence=0.8,
        evidence=evidence(1),
        events=[late, early],
    )
    resolved = EventResolver().resolve([scene], [])
    assert [event.description for event in resolved[0].events] == ["first", "second"]
    assert [event.sequence for event in resolved[0].events] == [1, 2]
