"""Backfill and constrain the canonical Story review state."""

import sqlalchemy as sa
from alembic import op

revision = "0011_canonical_review_states"
down_revision = "0010_story_audit_context"
branch_labels = None
depends_on = None

REVIEW_TABLES = (
    "characters",
    "scenes",
    "events",
    "story_relationships",
    "chapter_understandings",
    "story_versions",
)
STATES = ("needs_review", "confirmed", "rejected")


def upgrade():
    connection = op.get_bind()
    # Claims are embedded JSON rather than rows of their own. Normalize their
    # review state while the canonical table migration is applied.
    table = sa.table(
        "chapter_understandings", sa.column("id", sa.String), sa.column("claims", sa.JSON)
    )
    rows = connection.execute(sa.select(table.c.id, table.c.claims)).all()
    for row in rows:
        claims = row.claims or []
        changed = False
        for claim in claims:
            if claim.get("status") not in STATES:
                claim["status"] = "needs_review"
                changed = True
        if changed:
            connection.execute(
                table.update().where(table.c.id == row.id).values(claims=claims),
            )
    for table in REVIEW_TABLES:
        # Preserve human decisions; normalize NULL and every historical/unknown
        # value deterministically to the safe review queue.
        connection.execute(
            sa.text(
                f"UPDATE {table} SET status = CASE "
                "WHEN status IN ('confirmed', 'rejected') THEN status "
                "ELSE 'needs_review' END"
            )
        )
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "status",
                existing_type=sa.String(30),
                nullable=False,
                server_default="needs_review",
            )
            batch.create_check_constraint(
                f"ck_{table}_review_state",
                "status IN ('needs_review', 'confirmed', 'rejected')",
            )


def downgrade():
    for table in reversed(REVIEW_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"ck_{table}_review_state", type_="check")
