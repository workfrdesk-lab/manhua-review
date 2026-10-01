"""Add reconstructible actor and entity context to Story audit rows."""

import sqlalchemy as sa
from alembic import op

revision = "0010_story_audit_context"
down_revision = "0009_relationship_chapter_fks"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("story_audits", sa.Column("actor_id", sa.Uuid(), nullable=True))
    op.add_column("story_audits", sa.Column("entity_type", sa.String(40), nullable=True))
    op.add_column("story_audits", sa.Column("entity_id", sa.Uuid(), nullable=True))
    op.add_column("story_audits", sa.Column("version_id", sa.Uuid(), nullable=True))
    with op.batch_alter_table("story_audits") as batch:
        batch.create_foreign_key(
            "fk_story_audits_actor", "users", ["actor_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_foreign_key(
            "fk_story_audits_version",
            "story_versions",
            ["version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_story_audits_actor_id", ["actor_id"])
        batch.create_index("ix_story_audits_entity_id", ["entity_id"])
        batch.create_index("ix_story_audits_version_id", ["version_id"])


def downgrade():
    with op.batch_alter_table("story_audits") as batch:
        batch.drop_index("ix_story_audits_version_id")
        batch.drop_index("ix_story_audits_entity_id")
        batch.drop_index("ix_story_audits_actor_id")
        batch.drop_constraint("fk_story_audits_version", type_="foreignkey")
        batch.drop_constraint("fk_story_audits_actor", type_="foreignkey")
        batch.drop_column("version_id")
        batch.drop_column("entity_id")
        batch.drop_column("entity_type")
        batch.drop_column("actor_id")
