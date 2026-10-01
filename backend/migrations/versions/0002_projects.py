"""Projects and chapters with cascading ownership foreign keys."""

import sqlalchemy as sa
from alembic import op

revision = "0002_projects"
down_revision = "0001_auth"
branch_labels = None
depends_on = None


def upgrade():
    for table, owner, target in (
        ("projects", "user_id", "users.id"),
        ("chapters", "project_id", "projects.id"),
    ):
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(owner, sa.Uuid(), sa.ForeignKey(target, ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(f"ix_{table}_{owner}", table, [owner])


def downgrade():
    op.drop_table("chapters")
    op.drop_table("projects")
