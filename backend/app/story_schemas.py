from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.story_review import ReviewState


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_id: UUID | None = None
    page_number: int | None = Field(default=None, ge=1)
    panel_id: UUID | None = None
    ocr_result_id: UUID | None = None
    quote: str | None = Field(default=None, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def has_source(self):
        if not any((self.page_id, self.panel_id, self.ocr_result_id, self.page_number)):
            raise ValueError("evidence must identify a source")
        return self


Evidence = EvidenceRef

ReviewStatus = ReviewState


class AliasData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temp_id: str = Field(min_length=1)
    alias: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1)
    status: ReviewStatus = ReviewState.NEEDS_REVIEW
    evidence: list[EvidenceRef] = Field(min_length=1)


class SummaryClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temp_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    event_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    status: ReviewStatus = ReviewState.NEEDS_REVIEW
    evidence: list[EvidenceRef] = Field(min_length=1)


class ChapterSummaryData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[SummaryClaim]
    confidence: float = Field(ge=0, le=1)
    status: ReviewStatus = ReviewState.NEEDS_REVIEW
    evidence: list[EvidenceRef]


class CharacterData(BaseModel):
    temp_id: str = Field(min_length=1, max_length=100)
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    display_name: str | None = None
    description: str | None = None
    gender: str | None = None
    age_group: str | None = None
    importance: float = Field(ge=0, le=1)
    importance_reason: str | None = None
    first_appearance_page: int | None = Field(default=None, ge=1)
    confidence: float = Field(ge=0, le=1)
    aliases: list[AliasData] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(min_length=1)
    status: ReviewStatus = ReviewState.NEEDS_REVIEW


class EventData(BaseModel):
    temp_id: str = Field(min_length=1, max_length=100)
    character_refs: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=3000)
    event_type: str = "other"
    importance: float = Field(ge=0, le=1)
    importance_reason: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceRef] = Field(min_length=1)
    status: ReviewStatus = ReviewState.NEEDS_REVIEW
    page: int | None = Field(default=None, ge=1)
    panel: UUID | None = None
    sequence: int | None = Field(default=None, ge=1)


class SceneData(BaseModel):
    temp_id: str = Field(min_length=1, max_length=100)
    model_config = ConfigDict(extra="forbid")
    title: str
    summary: str
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    importance: float = Field(ge=0, le=1)
    importance_reason: str | None = None
    confidence: float = Field(ge=0, le=1)
    location: str | None = None
    character_refs: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(min_length=1)
    events: list[EventData] = Field(default_factory=list)
    status: ReviewStatus = ReviewState.NEEDS_REVIEW

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_page < self.start_page:
            raise ValueError("scene end_page must not precede start_page")
        return self


class RelationshipData(BaseModel):
    temp_id: str = Field(min_length=1, max_length=100)
    source_ref: str | None = None
    target_ref: str | None = None
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    relationship_type: str
    description: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceRef] = Field(min_length=1)
    status: ReviewStatus = ReviewState.NEEDS_REVIEW


class StoryStructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    characters: list[CharacterData] = Field(default_factory=list)
    scenes: list[SceneData] = Field(default_factory=list)
    relationships: list[RelationshipData] = Field(default_factory=list)
    logline: str = ""
    summary: str = ""
    main_characters: list[str] = Field(default_factory=list)
    major_events: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    revelations: list[str] = Field(default_factory=list)
    ending_state: str | None = None
    chapter_summary: ChapterSummaryData | None = None

    @model_validator(mode="before")
    @classmethod
    def not_empty(cls, value):
        if isinstance(value, dict) and not value:
            raise ValueError("story extraction must explicitly contain structured data")
        return value

    @field_validator("characters", "scenes", "relationships")
    @classmethod
    def bounded(cls, value):
        if len(value) > 200:
            raise ValueError("structured output contains too many objects")
        return value
