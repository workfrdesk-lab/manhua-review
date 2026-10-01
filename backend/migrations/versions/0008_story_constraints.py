"""Enforce uniqueness of story graph edges and ordering."""

from alembic import op

revision = "0008_story_constraints"
down_revision = "0007_story_integrity"
branch_labels = None
depends_on = None


def upgrade():
    for table, name, columns in (
        (
            "character_aliases",
            "uq_character_alias_normalized",
            ["character_id", "normalized_alias"],
        ),
        ("scenes", "uq_scene_chapter_index", ["chapter_id", "scene_index"]),
        ("events", "uq_event_scene_index", ["scene_id", "event_index"]),
        (
            "story_relationships",
            "uq_story_relationship_edge",
            ["chapter_id", "source_character_id", "target_character_id", "relationship_type"],
        ),
    ):
        with op.batch_alter_table(table) as batch:
            batch.create_unique_constraint(name, columns)


def downgrade():
    for table, name in (
        ("story_relationships", "uq_story_relationship_edge"),
        ("events", "uq_event_scene_index"),
        ("scenes", "uq_scene_chapter_index"),
        ("character_aliases", "uq_character_alias_normalized"),
    ):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="unique")
