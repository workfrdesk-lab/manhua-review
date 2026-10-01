"""Add fenced, lease-based ingestion attempts."""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0012_ingestion_attempt_recovery"
down_revision = "0011_canonical_review_states"
branch_labels = None
depends_on = None


ACTIVE = "'queued', 'extracting', 'processing_pages', 'finalizing'"


def upgrade() -> None:
    op.create_table(
        "ingestion_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("chapter_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("cleanup_status", sa.String(30), server_default="none", nullable=False),
        sa.Column("status", sa.String(30), server_default="queued", nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chapter_id", "generation"),
        sa.CheckConstraint(
            "status IN ('queued', 'extracting', 'processing_pages', 'finalizing', "
            "'completed', 'failed', 'abandoned', 'cancelled')",
            name="ck_ingestion_attempts_state",
        ),
    )
    op.create_index("ix_ingestion_attempts_job_id", "ingestion_attempts", ["job_id"])
    op.create_index("ix_ingestion_attempts_chapter_id", "ingestion_attempts", ["chapter_id"])
    op.create_index(
        "uq_ingestion_attempts_active_chapter",
        "ingestion_attempts",
        ["chapter_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({ACTIVE})"),
        sqlite_where=sa.text(f"status IN ({ACTIVE})"),
    )

    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("current_attempt_id", sa.Uuid(), nullable=True))
        batch.create_index("ix_jobs_current_attempt_id", ["current_attempt_id"])
        batch.create_foreign_key(
            "fk_jobs_current_attempt_id_ingestion_attempts",
            "ingestion_attempts",
            ["current_attempt_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("pages") as batch:
        batch.add_column(sa.Column("attempt_id", sa.Uuid(), nullable=True))
        batch.create_index("ix_pages_attempt_id", ["attempt_id"])
        batch.create_foreign_key(
            "fk_pages_attempt_id_ingestion_attempts",
            "ingestion_attempts",
            ["attempt_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            "uq_pages_attempt_page_number", ["attempt_id", "page_number"]
        )

    connection = op.get_bind()
    jobs = sa.table(
        "jobs",
        sa.column("id", sa.Uuid()),
        sa.column("chapter_id", sa.Uuid()),
        sa.column("status", sa.String()),
        sa.column("current_attempt_id", sa.Uuid()),
    )
    attempts = sa.table(
        "ingestion_attempts",
        sa.column("id", sa.Uuid()),
        sa.column("job_id", sa.Uuid()),
        sa.column("chapter_id", sa.Uuid()),
        sa.column("generation", sa.Integer()),
        sa.column("status", sa.String()),
    )
    pages = sa.table(
        "pages",
        sa.column("id", sa.Uuid()),
        sa.column("chapter_id", sa.Uuid()),
        sa.column("attempt_id", sa.Uuid()),
    )
    generations = {}
    for job_id, chapter_id, status in connection.execute(
        sa.select(jobs.c.id, jobs.c.chapter_id, jobs.c.status)
    ):
        attempt_id = uuid.uuid4()
        generation = generations.get(chapter_id, 0)
        generations[chapter_id] = generation - 1
        legacy_status = "completed" if status == "completed" else "failed"
        connection.execute(
            attempts.insert().values(
                id=attempt_id,
                job_id=job_id,
                chapter_id=chapter_id,
                generation=generation,
                status=legacy_status,
            )
        )
        connection.execute(
            jobs.update().where(jobs.c.id == job_id).values(current_attempt_id=attempt_id)
        )
        if generation == 0:
            connection.execute(
                pages.update().where(pages.c.chapter_id == chapter_id).values(attempt_id=attempt_id)
            )

    with op.batch_alter_table("pages") as batch:
        batch.alter_column("attempt_id", existing_type=sa.Uuid(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("pages") as batch:
        batch.drop_constraint("uq_pages_attempt_page_number", type_="unique")
        batch.drop_constraint("fk_pages_attempt_id_ingestion_attempts", type_="foreignkey")
        batch.drop_index("ix_pages_attempt_id")
        batch.drop_column("attempt_id")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("fk_jobs_current_attempt_id_ingestion_attempts", type_="foreignkey")
        batch.drop_index("ix_jobs_current_attempt_id")
        batch.drop_column("current_attempt_id")
    op.drop_index("uq_ingestion_attempts_active_chapter", table_name="ingestion_attempts")
    op.drop_index("ix_ingestion_attempts_chapter_id", table_name="ingestion_attempts")
    op.drop_index("ix_ingestion_attempts_job_id", table_name="ingestion_attempts")
    op.drop_table("ingestion_attempts")
