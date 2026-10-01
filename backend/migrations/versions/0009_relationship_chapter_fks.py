"""Prevent relationship edges from crossing chapter boundaries."""

from alembic import op

revision = "0009_relationship_chapter_fks"
down_revision = "0008_story_constraints"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("characters") as batch:
        batch.create_unique_constraint("uq_character_id_chapter", ["id", "chapter_id"])
    with op.batch_alter_table("story_relationships") as batch:
        batch.create_foreign_key(
            "fk_relationship_source_chapter",
            "characters",
            ["chapter_id", "source_character_id"],
            ["chapter_id", "id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_relationship_target_chapter",
            "characters",
            ["chapter_id", "target_character_id"],
            ["chapter_id", "id"],
            ondelete="CASCADE",
        )


def downgrade():
    with op.batch_alter_table("story_relationships") as batch:
        batch.drop_constraint("fk_relationship_target_chapter", type_="foreignkey")
        batch.drop_constraint("fk_relationship_source_chapter", type_="foreignkey")
    with op.batch_alter_table("characters") as batch:
        batch.drop_constraint("uq_character_id_chapter", type_="unique")
