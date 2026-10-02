"""Typed script contract. Snapshot references are StoryVersion temporary IDs, not live IDs."""

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.story_review import ReviewState
from app.story_schemas import EvidenceRef


class ScriptProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Stored legacy profiles may contain languages outside the 7B generation set.
    # New requests are restricted at the API boundary instead of breaking reads.
    language: str = Field(default="en", min_length=2, max_length=30)
    target_style: str = Field(default="chronological_recap", min_length=1, max_length=80)
    target_duration_seconds: int = Field(default=180, ge=15, le=3600)
    narration_tone: str = Field(default="neutral", min_length=1, max_length=80)
    preserve_source_dialogue: bool = False


class SegmentType(str, Enum):
    NARRATION = "narration"
    DIALOGUE = "dialogue"
    TRANSITION = "transition"
    INTRO = "intro"
    OUTRO = "outro"


class ScriptEvidenceData(EvidenceRef):
    # These references resolve exclusively inside the selected immutable proposal.
    scene_ref: str = Field(min_length=1, max_length=100)
    event_ref: str = Field(min_length=1, max_length=100)
    character_ref: str | None = Field(default=None, min_length=1, max_length=100)


class ScriptSegmentData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temp_id: str = Field(min_length=1, max_length=100)
    sequence: int = Field(ge=1)
    segment_type: SegmentType = SegmentType.NARRATION
    narration_text: str = Field(min_length=1, max_length=5000)
    dialogue_text: str | None = Field(default=None, min_length=1, max_length=1000)
    source_dialogue: bool = False
    speaker_ref: str | None = Field(default=None, min_length=1, max_length=100)
    scene_ref: str = Field(min_length=1, max_length=100)
    event_refs: list[str] = Field(min_length=1, max_length=100)
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    estimated_duration: float = Field(gt=0, le=3600, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    status: ReviewState = ReviewState.NEEDS_REVIEW
    evidence: list[ScriptEvidenceData] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def coherent(self):
        if self.end_page < self.start_page:
            raise ValueError("invalid page range")
        if bool(self.dialogue_text) != self.source_dialogue:
            raise ValueError("dialogue must be explicitly source dialogue")
        if (self.segment_type == SegmentType.DIALOGUE) != self.source_dialogue:
            raise ValueError("source dialogue requires the dialogue segment type")
        if self.speaker_ref and not self.source_dialogue:
            raise ValueError("narration paraphrases cannot be attributed as dialogue")
        if len(set(self.event_refs)) != len(self.event_refs):
            raise ValueError("duplicate event references")
        return self


class ScriptStructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    # Front matter repeats grounded segment text; it is not an evidence loophole.
    hook: str = Field(default="", max_length=5000)
    intro: str = Field(default="", max_length=5000)
    outro: str = Field(default="", max_length=5000)
    status: ReviewState = ReviewState.NEEDS_REVIEW
    segments: list[ScriptSegmentData] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def ordered(self):
        if [s.sequence for s in self.segments] != list(range(1, len(self.segments) + 1)):
            raise ValueError("segments must be contiguous and ordered starting at one")
        if len({s.temp_id for s in self.segments}) != len(self.segments):
            raise ValueError("duplicate segment IDs")
        return self


class GenerateScriptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    story_version_id: UUID
    profile: ScriptProfile = Field(default_factory=ScriptProfile)
