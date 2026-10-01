"""Persist grounded scripts tied to immutable StoryVersions."""

import sqlalchemy as sa
from alembic import op

revision = "0013_script_domain"
down_revision = "0012_ingestion_attempt_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("story_versions") as batch:
        batch.create_unique_constraint("uq_story_version_id_chapter", ["id", "chapter_id"])
    op.create_table(
        "script_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chapter_id", sa.Uuid(), nullable=False),
        sa.Column("story_version_id", sa.Uuid(), nullable=False),
        sa.Column("profile_fingerprint", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("profile", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="needs_review"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["story_version_id", "chapter_id"],
            ["story_versions.id", "story_versions.chapter_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "story_version_id", "profile_fingerprint", name="uq_script_version_profile"
        ),
        sa.UniqueConstraint("id", "chapter_id", name="uq_script_version_id_chapter"),
        sa.CheckConstraint(
            "status IN ('needs_review', 'confirmed', 'rejected')", name="story_review_state"
        ),
    )
    op.create_index("ix_script_versions_chapter_id", "script_versions", ["chapter_id"])
    op.create_index("ix_script_versions_story_version_id", "script_versions", ["story_version_id"])
    op.create_table(
        "script_segments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("chapter_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("segment_type", sa.String(30), nullable=False),
        sa.Column("narration_text", sa.String(5000), nullable=False),
        sa.Column("dialogue_text", sa.String(1000)),
        sa.Column("scene_ref", sa.String(100), nullable=False),
        sa.Column("event_refs", sa.JSON(), nullable=False),
        sa.Column("start_page", sa.Integer(), nullable=False),
        sa.Column("end_page", sa.Integer(), nullable=False),
        sa.Column("estimated_duration", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="needs_review"),
        sa.ForeignKeyConstraint(
            ["script_version_id", "chapter_id"],
            ["script_versions.id", "script_versions.chapter_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("script_version_id", "sequence", name="uq_script_segment_sequence"),
        sa.UniqueConstraint("id", "script_version_id", name="uq_script_segment_id_version"),
        sa.CheckConstraint(
            "status IN ('needs_review', 'confirmed', 'rejected')", name="ck_script_segment_review"
        ),
    )
    op.create_index(
        "ix_script_segments_script_version_id", "script_segments", ["script_version_id"]
    )
    op.create_index("ix_script_segments_chapter_id", "script_segments", ["chapter_id"])
    op.create_table(
        "script_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("segment_id", sa.Uuid(), nullable=False),
        sa.Column("chapter_id", sa.Uuid(), nullable=False),
        sa.Column("scene_ref", sa.String(100), nullable=False),
        sa.Column("event_ref", sa.String(100), nullable=False),
        sa.Column("character_ref", sa.String(100)),
        sa.Column("page_id", sa.Uuid()),
        sa.Column("page_number", sa.Integer()),
        sa.Column("panel_id", sa.Uuid()),
        sa.Column("ocr_result_id", sa.Uuid()),
        sa.Column("quote", sa.String(1000)),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.ForeignKeyConstraint(
            ["script_version_id", "chapter_id"],
            ["script_versions.id", "script_versions.chapter_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id", "script_version_id"],
            ["script_segments.id", "script_segments.script_version_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["panel_id"], ["panels.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ocr_result_id"], ["ocr_results.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_script_evidence_script_version_id", "script_evidence", ["script_version_id"]
    )
    op.create_index("ix_script_evidence_segment_id", "script_evidence", ["segment_id"])
    op.create_index("ix_script_evidence_chapter_id", "script_evidence", ["chapter_id"])


def downgrade() -> None:
    op.drop_table("script_evidence")
    op.drop_table("script_segments")
    op.drop_table("script_versions")
    with op.batch_alter_table("story_versions") as batch:
        batch.drop_constraint("uq_story_version_id_chapter", type_="unique")
