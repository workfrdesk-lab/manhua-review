from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.story_review import ReviewState


class Base(DeclarativeBase):
    pass


UUIDType = Uuid(as_uuid=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    user: Mapped[User] = relationship(back_populates="sessions", lazy="joined")


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("provider", "provider_subject"),)

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    provider_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="draft", server_default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="draft", server_default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    files: Mapped[list["ChapterFile"]] = relationship(
        back_populates="chapter", cascade="all, delete-orphan"
    )
    pages: Mapped[list["Page"]] = relationship(
        back_populates="chapter", cascade="all, delete-orphan", order_by="Page.page_number"
    )


class ChapterFile(Base):
    __tablename__ = "chapter_files"

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    original_name: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(1000), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    chapter: Mapped[Chapter] = relationship(back_populates="files")


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("attempt_id", "page_number"),
        UniqueConstraint("id", "chapter_id", name="uq_page_id_chapter"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingestion_attempts.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(1000), unique=True)
    thumbnail_key: Mapped[str] = mapped_column(String(1000), unique=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(10))
    file_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    chapter: Mapped[Chapter] = relationship(back_populates="pages")
    attempt: Mapped["IngestionAttempt"] = relationship(back_populates="pages")
    panels: Mapped[list["Panel"]] = relationship(
        back_populates="page", cascade="all, delete-orphan", order_by="Panel.reading_order"
    )


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), unique=True)
    reading_direction: Mapped[str] = mapped_column(String(3), default="ltr", server_default="ltr")
    status: Mapped[str] = mapped_column(String(30), default="queued", server_default="queued")
    panel_detection_status: Mapped[str] = mapped_column(
        String(30), default="queued", server_default="queued"
    )
    ocr_status: Mapped[str] = mapped_column(String(30), default="queued", server_default="queued")
    visual_analysis_status: Mapped[str] = mapped_column(
        String(30), default="queued", server_default="queued"
    )
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    page: Mapped[Page] = relationship()


class Panel(Base):
    __tablename__ = "panels"
    __table_args__ = (UniqueConstraint("id", "page_id", name="uq_panel_id_page"),)
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    panel_index: Mapped[int] = mapped_column(Integer)
    x: Mapped[int] = mapped_column(Integer)
    y: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    x_norm: Mapped[float] = mapped_column(Float)
    y_norm: Mapped[float] = mapped_column(Float)
    width_norm: Mapped[float] = mapped_column(Float)
    height_norm: Mapped[float] = mapped_column(Float)
    reading_order: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="detected", server_default="detected")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    page: Mapped[Page] = relationship(back_populates="panels")
    ocr_results: Mapped[list["OCRResult"]] = relationship(cascade="all, delete-orphan")
    visual_analysis: Mapped["VisualAnalysis | None"] = relationship(
        cascade="all, delete-orphan", uselist=False
    )


class OCRResult(Base):
    __tablename__ = "ocr_results"
    __table_args__ = (UniqueConstraint("id", "panel_id", name="uq_ocr_id_panel"),)
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    panel_id: Mapped[UUID] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(10000), default="")
    language: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="completed", server_default="completed")
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VisualAnalysis(Base):
    __tablename__ = "visual_analysis"
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    panel_id: Mapped[UUID] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"), unique=True)
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="completed", server_default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StoryAnalysisJob(Base):
    __tablename__ = "story_analysis_jobs"
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    pages_processed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    chunks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    validation_errors: Mapped[list] = mapped_column(JSON, default=list)
    validation_warnings: Mapped[list] = mapped_column(JSON, default=list)
    cost_status: Mapped[str] = mapped_column(
        String(20), default="unavailable", server_default="unavailable"
    )
    analysis_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StoryReviewMixin:
    provenance: Mapped[str] = mapped_column(
        String(30), default="ai_generated", server_default="ai_generated"
    )
    edited_fields: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    review_conflicts: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")


class StoryVersion(Base):
    __tablename__ = "story_versions"
    __table_args__ = (
        UniqueConstraint("chapter_id", "fingerprint", name="uq_story_version_source"),
        UniqueConstraint("id", "chapter_id", name="uq_story_version_id_chapter"),
    )
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)
    decisions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[ReviewState] = mapped_column(
        SAEnum(
            ReviewState,
            values_callable=lambda enum: [state.value for state in enum],
            native_enum=False,
            length=30,
            name="story_review_state",
            create_constraint=True,
        ),
        default=ReviewState.NEEDS_REVIEW,
        server_default=ReviewState.NEEDS_REVIEW.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StoryAudit(Base):
    __tablename__ = "story_audits"
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("story_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScriptGenerationJob(Base):
    __tablename__ = "script_generation_jobs"
    __table_args__ = (
        UniqueConstraint(
            "story_version_id", "profile_fingerprint", "attempt", name="uq_script_job_attempt"
        ),
        ForeignKeyConstraint(
            ["story_version_id", "chapter_id"],
            ["story_versions.id", "story_versions.chapter_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_script_job_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"))
    story_version_id: Mapped[UUID] = mapped_column(UUIDType)
    script_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("script_versions.id", ondelete="SET NULL"), nullable=True
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    profile_fingerprint: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict] = mapped_column(JSON)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScriptVersion(Base):
    __tablename__ = "script_versions"
    __table_args__ = (
        UniqueConstraint("id", "chapter_id", name="uq_script_version_id_chapter"),
        UniqueConstraint(
            "story_version_id", "profile_fingerprint", name="uq_script_version_profile"
        ),
        ForeignKeyConstraint(
            ["story_version_id", "chapter_id"],
            ["story_versions.id", "story_versions.chapter_id"],
            ondelete="CASCADE",
            name="fk_script_version_story_snapshot",
        ),
    )
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    story_version_id: Mapped[UUID] = mapped_column(index=True)
    profile_fingerprint: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[ReviewState] = mapped_column(
        SAEnum(
            ReviewState,
            values_callable=lambda e: [s.value for s in e],
            native_enum=False,
            length=30,
            name="story_review_state",
            create_constraint=True,
        ),
        default=ReviewState.NEEDS_REVIEW,
        server_default=ReviewState.NEEDS_REVIEW.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScriptSegment(Base):
    __tablename__ = "script_segments"
    __table_args__ = (
        UniqueConstraint("id", "script_version_id", name="uq_script_segment_id_version"),
        CheckConstraint(
            "status IN ('needs_review', 'confirmed', 'rejected')", name="ck_script_segment_review"
        ),
        UniqueConstraint("script_version_id", "sequence", name="uq_script_segment_sequence"),
        ForeignKeyConstraint(
            ["script_version_id", "chapter_id"],
            ["script_versions.id", "script_versions.chapter_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    script_version_id: Mapped[UUID] = mapped_column(index=True)
    chapter_id: Mapped[UUID] = mapped_column(index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    segment_type: Mapped[str] = mapped_column(String(30))
    narration_text: Mapped[str] = mapped_column(String(5000))
    dialogue_text: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    scene_ref: Mapped[str] = mapped_column(String(100))
    event_refs: Mapped[list] = mapped_column(JSON)
    start_page: Mapped[int] = mapped_column(Integer)
    end_page: Mapped[int] = mapped_column(Integer)
    estimated_duration: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[ReviewState] = mapped_column(
        String(30),
        default=ReviewState.NEEDS_REVIEW.value,
        server_default=ReviewState.NEEDS_REVIEW.value,
    )


class ScriptEvidence(Base):
    __tablename__ = "script_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["page_id", "chapter_id"],
            ["pages.id", "pages.chapter_id"],
            name="fk_evidence_page_chapter",
        ),
        ForeignKeyConstraint(
            ["panel_id", "page_id"], ["panels.id", "panels.page_id"], name="fk_evidence_panel_page"
        ),
        ForeignKeyConstraint(
            ["ocr_result_id", "panel_id"],
            ["ocr_results.id", "ocr_results.panel_id"],
            name="fk_evidence_ocr_panel",
        ),
        CheckConstraint(
            "page_id IS NOT NULL AND (ocr_result_id IS NULL OR panel_id IS NOT NULL)",
            name="ck_script_evidence_source",
        ),
        ForeignKeyConstraint(
            ["script_version_id", "chapter_id"],
            ["script_versions.id", "script_versions.chapter_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["segment_id", "script_version_id"],
            ["script_segments.id", "script_segments.script_version_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    script_version_id: Mapped[UUID] = mapped_column(index=True)
    segment_id: Mapped[UUID] = mapped_column(index=True)
    chapter_id: Mapped[UUID] = mapped_column(index=True)
    scene_ref: Mapped[str] = mapped_column(String(100))
    event_ref: Mapped[str] = mapped_column(String(100))
    character_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    page_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("pages.id", ondelete="SET NULL"), nullable=True
    )
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    panel_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("panels.id", ondelete="SET NULL"), nullable=True
    )
    ocr_result_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ocr_results.id", ondelete="SET NULL"), nullable=True
    )
    quote: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    reason: Mapped[str] = mapped_column(String(500))


class SceneCharacter(Base):
    __tablename__ = "scene_characters"
    scene_id: Mapped[UUID] = mapped_column(
        ForeignKey("scenes.id", ondelete="CASCADE"), primary_key=True
    )
    character_id: Mapped[UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True
    )


class EventCharacter(Base):
    __tablename__ = "event_characters"
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    character_id: Mapped[UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True
    )


class Character(StoryReviewMixin, Base):
    __tablename__ = "characters"
    __table_args__ = (UniqueConstraint("id", "chapter_id", name="uq_character_id_chapter"),)
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(80), nullable=True)
    age_group: Mapped[str | None] = mapped_column(String(80), nullable=True)
    importance: Mapped[float] = mapped_column(Float, default=0.0)
    importance_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    first_appearance_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[ReviewState] = mapped_column(
        SAEnum(
            ReviewState,
            values_callable=lambda enum: [state.value for state in enum],
            native_enum=False,
            length=30,
            name="story_review_state",
            create_constraint=True,
        ),
        default=ReviewState.NEEDS_REVIEW,
        server_default=ReviewState.NEEDS_REVIEW.value,
        nullable=False,
    )
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CharacterAlias(Base):
    __tablename__ = "character_aliases"
    __table_args__ = (
        UniqueConstraint("character_id", "normalized_alias", name="uq_character_alias_normalized"),
    )
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    character_id: Mapped[UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(80), default="llm")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    normalized_alias: Mapped[str] = mapped_column(String(200), default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)


class Scene(StoryReviewMixin, Base):
    __tablename__ = "scenes"
    __table_args__ = (UniqueConstraint("chapter_id", "scene_index", name="uq_scene_chapter_index"),)
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    scene_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(String(5000))
    start_page: Mapped[int] = mapped_column(Integer)
    end_page: Mapped[int] = mapped_column(Integer)
    importance: Mapped[float] = mapped_column(Float, default=0.0)
    importance_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[ReviewState] = mapped_column(
        SAEnum(
            ReviewState,
            values_callable=lambda enum: [state.value for state in enum],
            native_enum=False,
            length=30,
            name="story_review_state",
            create_constraint=True,
        ),
        default=ReviewState.NEEDS_REVIEW,
        server_default=ReviewState.NEEDS_REVIEW.value,
        nullable=False,
    )
    evidence: Mapped[list] = mapped_column(JSON, default=list)


class Event(StoryReviewMixin, Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("scene_id", "event_index", name="uq_event_scene_index"),)
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    scene_id: Mapped[UUID] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    page_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("pages.id", ondelete="SET NULL"), nullable=True
    )
    event_index: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(3000))
    event_type: Mapped[str] = mapped_column(String(60), default="other")
    importance: Mapped[float] = mapped_column(Float, default=0.0)
    importance_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    panel_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("panels.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[ReviewState] = mapped_column(
        SAEnum(
            ReviewState,
            values_callable=lambda enum: [state.value for state in enum],
            native_enum=False,
            length=30,
            name="story_review_state",
            create_constraint=True,
        ),
        default=ReviewState.NEEDS_REVIEW,
        server_default=ReviewState.NEEDS_REVIEW.value,
        nullable=False,
    )
    evidence: Mapped[list] = mapped_column(JSON, default=list)


class StoryRelationship(StoryReviewMixin, Base):
    __tablename__ = "story_relationships"
    __table_args__ = (
        UniqueConstraint(
            "chapter_id",
            "source_character_id",
            "target_character_id",
            "relationship_type",
            name="uq_story_relationship_edge",
        ),
        ForeignKeyConstraint(
            ["chapter_id", "source_character_id"],
            ["characters.chapter_id", "characters.id"],
            name="fk_relationship_source_chapter",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["chapter_id", "target_character_id"],
            ["characters.chapter_id", "characters.id"],
            name="fk_relationship_target_chapter",
            ondelete="CASCADE",
        ),
    )
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    source_character_id: Mapped[UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE")
    )
    target_character_id: Mapped[UUID] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE")
    )
    relationship_type: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[ReviewState] = mapped_column(
        SAEnum(
            ReviewState,
            values_callable=lambda enum: [state.value for state in enum],
            native_enum=False,
            length=30,
            name="story_review_state",
            create_constraint=True,
        ),
        default=ReviewState.NEEDS_REVIEW,
        server_default=ReviewState.NEEDS_REVIEW.value,
        nullable=False,
    )
    evidence: Mapped[list] = mapped_column(JSON, default=list)


class ChapterUnderstanding(StoryReviewMixin, Base):
    __tablename__ = "chapter_understandings"
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), unique=True
    )
    logline: Mapped[str] = mapped_column(String(2000), default="")
    summary: Mapped[str] = mapped_column(String(10000), default="")
    main_characters: Mapped[list] = mapped_column(JSON, default=list)
    major_events: Mapped[list] = mapped_column(JSON, default=list)
    conflicts: Mapped[list] = mapped_column(JSON, default=list)
    revelations: Mapped[list] = mapped_column(JSON, default=list)
    ending_state: Mapped[str | None] = mapped_column(String(3000), nullable=True)
    status: Mapped[ReviewState] = mapped_column(
        SAEnum(
            ReviewState,
            values_callable=lambda enum: [state.value for state in enum],
            native_enum=False,
            length=30,
            name="story_review_state",
            create_constraint=True,
        ),
        default=ReviewState.NEEDS_REVIEW,
        server_default=ReviewState.NEEDS_REVIEW.value,
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    evidence: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    claims: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[UUID] = mapped_column(ForeignKey("chapter_files.id", ondelete="CASCADE"))
    current_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ingestion_attempts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="queued", server_default="queued")
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionAttempt(Base):
    __tablename__ = "ingestion_attempts"
    __table_args__ = (
        UniqueConstraint("chapter_id", "generation"),
        CheckConstraint(
            "status IN ('queued', 'extracting', 'processing_pages', 'finalizing', "
            "'completed', 'failed', 'abandoned', 'cancelled')",
            name="ck_ingestion_attempts_state",
        ),
        Index(
            "uq_ingestion_attempts_active_chapter",
            "chapter_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued', 'extracting', 'processing_pages', 'finalizing')"
            ),
            sqlite_where=text(
                "status IN ('queued', 'extracting', 'processing_pages', 'finalizing')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    cleanup_status: Mapped[str] = mapped_column(String(30), default="none", server_default="none")
    status: Mapped[str] = mapped_column(String(30), default="queued", server_default="queued")
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    pages: Mapped[list[Page]] = relationship(back_populates="attempt", cascade="all, delete-orphan")
