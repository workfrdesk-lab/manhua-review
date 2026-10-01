"""Add evidence, review, cost, and story reference fields."""

import sqlalchemy as sa
from alembic import op

revision = "0006_story_review"
down_revision = "0005_story"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "story_analysis_jobs",
        sa.Column("validation_errors", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "story_analysis_jobs",
        sa.Column("validation_warnings", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "story_analysis_jobs",
        sa.Column("cost_status", sa.String(20), nullable=False, server_default="unavailable"),
    )
    op.add_column("story_analysis_jobs", sa.Column("analysis_config_fingerprint", sa.String(64)))
    op.add_column(
        "characters",
        sa.Column("status", sa.String(30), nullable=False, server_default="needs_review"),
    )
    op.add_column(
        "characters", sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column(
        "character_aliases",
        sa.Column("normalized_alias", sa.String(200), nullable=False, server_default=""),
    )
    op.add_column(
        "character_aliases", sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column("scenes", sa.Column("location", sa.String(500)))
    op.add_column(
        "scenes", sa.Column("status", sa.String(30), nullable=False, server_default="needs_review")
    )
    op.add_column("scenes", sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"))
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("panel_id", sa.Uuid()))
        batch.create_foreign_key(
            "fk_events_panel_id", "panels", ["panel_id"], ["id"], ondelete="SET NULL"
        )
    op.add_column(
        "events", sa.Column("status", sa.String(30), nullable=False, server_default="needs_review")
    )
    op.add_column("events", sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column(
        "story_relationships",
        sa.Column("status", sa.String(30), nullable=False, server_default="needs_review"),
    )
    op.add_column(
        "story_relationships", sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]")
    )


def downgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint("fk_events_panel_id", type_="foreignkey")
    for table, columns in {
        "story_relationships": ["evidence", "status"],
        "events": ["evidence", "status", "panel_id"],
        "scenes": ["evidence", "status", "location"],
        "character_aliases": ["evidence", "normalized_alias"],
        "characters": ["evidence", "status"],
        "story_analysis_jobs": [
            "analysis_config_fingerprint",
            "cost_status",
            "validation_warnings",
            "validation_errors",
        ],
    }.items():
        for column in columns:
            op.drop_column(table, column)
