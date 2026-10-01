"""Persist script generation attempts without replacing the existing queue."""

import sqlalchemy as sa
from alembic import op

revision = "0014_script_jobs"
down_revision = "0013_script_domain"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "script_generation_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chapter_id", sa.Uuid(), nullable=False),
        sa.Column("story_version_id", sa.Uuid(), nullable=False),
        sa.Column("script_version_id", sa.Uuid()),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("profile_fingerprint", sa.String(64), nullable=False),
        sa.Column("profile", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["story_version_id", "chapter_id"],
            ["story_versions.id", "story_versions.chapter_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["script_version_id"], ["script_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "story_version_id", "profile_fingerprint", "attempt", name="uq_script_job_attempt"
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_script_job_status",
        ),
    )


def downgrade():
    op.drop_table("script_generation_jobs")
