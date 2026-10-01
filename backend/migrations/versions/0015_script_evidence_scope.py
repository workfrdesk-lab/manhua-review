"""Add chapter-scoped composite source integrity for script evidence."""

from alembic import op

revision = "0015_script_evidence_scope"
down_revision = "0014_script_jobs"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("pages") as batch:
        batch.create_unique_constraint("uq_page_id_chapter", ["id", "chapter_id"])
    with op.batch_alter_table("panels") as batch:
        batch.create_unique_constraint("uq_panel_id_page", ["id", "page_id"])
    with op.batch_alter_table("ocr_results") as batch:
        batch.create_unique_constraint("uq_ocr_id_panel", ["id", "panel_id"])
    with op.batch_alter_table("script_evidence") as batch:
        batch.create_foreign_key(
            "fk_evidence_page_chapter", "pages", ["page_id", "chapter_id"], ["id", "chapter_id"]
        )
        batch.create_foreign_key(
            "fk_evidence_panel_page", "panels", ["panel_id", "page_id"], ["id", "page_id"]
        )
        batch.create_foreign_key(
            "fk_evidence_ocr_panel",
            "ocr_results",
            ["ocr_result_id", "panel_id"],
            ["id", "panel_id"],
        )
        batch.create_check_constraint(
            "ck_script_evidence_source",
            "page_id IS NOT NULL AND (ocr_result_id IS NULL OR panel_id IS NOT NULL)",
        )


def downgrade():
    with op.batch_alter_table("script_evidence") as batch:
        batch.drop_constraint("ck_script_evidence_source", type_="check")
        batch.drop_constraint("fk_evidence_ocr_panel", type_="foreignkey")
        batch.drop_constraint("fk_evidence_panel_page", type_="foreignkey")
        batch.drop_constraint("fk_evidence_page_chapter", type_="foreignkey")
    with op.batch_alter_table("ocr_results") as batch:
        batch.drop_constraint("uq_ocr_id_panel", type_="unique")
    with op.batch_alter_table("panels") as batch:
        batch.drop_constraint("uq_panel_id_page", type_="unique")
    with op.batch_alter_table("pages") as batch:
        batch.drop_constraint("uq_page_id_chapter", type_="unique")
