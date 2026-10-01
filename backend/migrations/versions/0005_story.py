"""Story understanding data."""

import sqlalchemy as sa
from alembic import op

revision = "0005_story"
down_revision = "0004_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = sa.Uuid()

    def common():
        return [sa.Column("id", sa.Uuid(), primary_key=True)]

    op.create_table(
        "story_analysis_jobs",
        *common(),
        sa.Column(
            "chapter_id",
            uuid,
            sa.ForeignKey("chapters.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("provider", sa.String(40)),
        sa.Column("model", sa.String(120)),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("pages_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duration", sa.Float()),
        sa.Column("source_fingerprint", sa.String(64)),
        sa.Column("error", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "characters",
        *common(),
        sa.Column(
            "chapter_id", uuid, sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000)),
        sa.Column("gender", sa.String(80)),
        sa.Column("age_group", sa.String(80)),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0"),
        sa.Column("importance_reason", sa.String(500)),
        sa.Column("first_appearance_page", sa.Integer()),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_characters_chapter_id", "characters", ["chapter_id"])
    op.create_table(
        "character_aliases",
        *common(),
        sa.Column(
            "character_id", uuid, sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("alias", sa.String(200), nullable=False),
        sa.Column("source", sa.String(80), nullable=False, server_default="llm"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_character_aliases_character_id", "character_aliases", ["character_id"])
    op.create_table(
        "scenes",
        *common(),
        sa.Column(
            "chapter_id", uuid, sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scene_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.String(5000), nullable=False),
        sa.Column("start_page", sa.Integer(), nullable=False),
        sa.Column("end_page", sa.Integer(), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0"),
        sa.Column("importance_reason", sa.String(500)),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_scenes_chapter_id", "scenes", ["chapter_id"])
    op.create_table(
        "events",
        *common(),
        sa.Column("scene_id", uuid, sa.ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", uuid, sa.ForeignKey("pages.id", ondelete="SET NULL")),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(3000), nullable=False),
        sa.Column("event_type", sa.String(60), nullable=False, server_default="other"),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0"),
        sa.Column("importance_reason", sa.String(500)),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_events_scene_id", "events", ["scene_id"])
    op.create_table(
        "story_relationships",
        *common(),
        sa.Column(
            "chapter_id", uuid, sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "source_character_id",
            uuid,
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_character_id",
            uuid,
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relationship_type", sa.String(120), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_story_relationships_chapter_id", "story_relationships", ["chapter_id"])
    op.create_table(
        "chapter_understandings",
        *common(),
        sa.Column(
            "chapter_id",
            uuid,
            sa.ForeignKey("chapters.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("logline", sa.String(2000), nullable=False, server_default=""),
        sa.Column("summary", sa.String(10000), nullable=False, server_default=""),
        sa.Column("main_characters", sa.JSON(), nullable=False),
        sa.Column("major_events", sa.JSON(), nullable=False),
        sa.Column("conflicts", sa.JSON(), nullable=False),
        sa.Column("revelations", sa.JSON(), nullable=False),
        sa.Column("ending_state", sa.String(3000)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    for table in (
        "chapter_understandings",
        "story_relationships",
        "events",
        "scenes",
        "character_aliases",
        "characters",
        "story_analysis_jobs",
    ):
        op.drop_table(table)
