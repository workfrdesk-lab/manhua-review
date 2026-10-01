"""Independent page analysis results."""

import sqlalchemy as sa
from alembic import op

revision = "0004_analysis"
down_revision = "0003_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "page_id",
            sa.Uuid(),
            sa.ForeignKey("pages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("reading_direction", sa.String(3), nullable=False, server_default="ltr"),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("panel_detection_status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("ocr_status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("visual_analysis_status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("error", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "panels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "page_id", sa.Uuid(), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("panel_index", sa.Integer(), nullable=False),
        sa.Column("x", sa.Integer(), nullable=False),
        sa.Column("y", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("x_norm", sa.Float(), nullable=False),
        sa.Column("y_norm", sa.Float(), nullable=False),
        sa.Column("width_norm", sa.Float(), nullable=False),
        sa.Column("height_norm", sa.Float(), nullable=False),
        sa.Column("reading_order", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="detected"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_panels_page_id", "panels", ["page_id"])
    op.create_table(
        "ocr_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "panel_id", sa.Uuid(), sa.ForeignKey("panels.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("text", sa.String(10000), nullable=False),
        sa.Column("language", sa.String(30)),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="completed"),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_ocr_results_panel_id", "ocr_results", ["panel_id"])
    op.create_table(
        "visual_analysis",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "panel_id", sa.Uuid(), sa.ForeignKey("panels.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="completed"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("panel_id"),
    )


def downgrade() -> None:
    op.drop_table("visual_analysis")
    op.drop_table("ocr_results")
    op.drop_table("panels")
    op.drop_table("analysis_jobs")
