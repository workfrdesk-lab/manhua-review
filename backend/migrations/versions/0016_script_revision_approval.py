"""Add revision-aware script approval binding."""

import sqlalchemy as sa
from alembic import op

revision = "0016_script_revision_approval"
down_revision = "0015_script_evidence_scope"
branch_labels = None
depends_on = None


def upgrade():
    # Column defaults establish the baseline without updating legacy rows or timestamps.
    with op.batch_alter_table("script_versions") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("approved_revision", sa.Integer(), nullable=True))
        batch.create_check_constraint("ck_script_revision_positive", "revision > 0")
        batch.create_check_constraint(
            "ck_script_approved_revision",
            "approved_revision IS NULL OR (approved_revision = revision AND status = 'confirmed')",
        )


def downgrade():
    # Downgrade loses revision semantics and requires all 7B consumers to be stopped.
    with op.batch_alter_table("script_versions") as batch:
        batch.drop_constraint("ck_script_approved_revision", type_="check")
        batch.drop_constraint("ck_script_revision_positive", type_="check")
        batch.drop_column("approved_revision")
        batch.drop_column("revision")
