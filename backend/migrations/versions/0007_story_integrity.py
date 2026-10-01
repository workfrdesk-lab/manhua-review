"""Immutable analysis proposals, review provenance, audit and participation edges."""

import sqlalchemy as sa
from alembic import op

revision = "0007_story_integrity"
down_revision = "0006_story_review"
branch_labels = None
depends_on = None

REVIEW_TABLES = ("characters", "scenes", "events", "story_relationships", "chapter_understandings")


def upgrade():
    for table in REVIEW_TABLES:
        op.add_column(
            table,
            sa.Column("provenance", sa.String(30), nullable=False, server_default="ai_generated"),
        )
        for name in ("edited_fields", "review_conflicts"):
            op.add_column(table, sa.Column(name, sa.JSON(), nullable=False, server_default="[]"))
    for name in ("evidence", "claims"):
        op.add_column(
            "chapter_understandings",
            sa.Column(name, sa.JSON(), nullable=False, server_default="[]"),
        )
    op.add_column(
        "chapter_understandings",
        sa.Column("status", sa.String(30), nullable=False, server_default="needs_review"),
    )
    op.add_column(
        "chapter_understandings",
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
    )
    for table in ("story_versions", "story_audits"):
        columns = [
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "chapter_id",
                sa.Uuid(),
                sa.ForeignKey("chapters.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        ]
        if table == "story_versions":
            columns.extend(
                [
                    sa.Column("fingerprint", sa.String(64), nullable=False),
                    sa.Column("decisions", sa.JSON(), nullable=False),
                    sa.Column("status", sa.String(30), nullable=False),
                    sa.UniqueConstraint(
                        "chapter_id", "fingerprint", name="uq_story_version_source"
                    ),
                ]
            )
        else:
            columns.append(sa.Column("action", sa.String(30), nullable=False))
        op.create_table(table, *columns)
        op.create_index(f"ix_{table}_chapter_id", table, ["chapter_id"])
    for table, owner in (("scene_characters", "scene"), ("event_characters", "event")):
        op.create_table(
            table,
            sa.Column(
                f"{owner}_id",
                sa.Uuid(),
                sa.ForeignKey(f"{owner}s.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "character_id",
                sa.Uuid(),
                sa.ForeignKey("characters.id", ondelete="CASCADE"),
                primary_key=True,
            ),
        )


def downgrade():
    for table in ("event_characters", "scene_characters", "story_audits", "story_versions"):
        op.drop_table(table)
    for name in ("confidence", "status", "claims", "evidence"):
        op.drop_column("chapter_understandings", name)
    for table in REVIEW_TABLES:
        for name in ("review_conflicts", "edited_fields", "provenance"):
            op.drop_column(table, name)
