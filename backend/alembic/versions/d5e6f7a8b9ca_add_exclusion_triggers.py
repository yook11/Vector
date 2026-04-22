"""enforce mutual exclusion between article_analyses and article_rejections.

同一 extraction に対して analyses と rejections の両方が存在する状態を防ぐ。
アプリケーション層（ClassificationService）でも冪等チェックしているが、
DB トリガーは第二層の防御として残す（案 A）。

Revision ID: d5e6f7a8b9ca
Revises: d7a8b9c0d1e2
Create Date: 2026-04-22
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9ca"
down_revision: str | None = "d7a8b9c0d1e2"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_rejection_for_analysis()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM article_rejections
                WHERE extraction_id = NEW.extraction_id
            ) THEN
                RAISE EXCEPTION 'extraction % already has a rejection', NEW.extraction_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_article_analyses_no_rejection
        BEFORE INSERT OR UPDATE ON article_analyses
        FOR EACH ROW EXECUTE FUNCTION enforce_no_rejection_for_analysis();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_analysis_for_rejection()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM article_analyses
                WHERE extraction_id = NEW.extraction_id
            ) THEN
                RAISE EXCEPTION 'extraction % already has an analysis', NEW.extraction_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_article_rejections_no_analysis
        BEFORE INSERT OR UPDATE ON article_rejections
        FOR EACH ROW EXECUTE FUNCTION enforce_no_analysis_for_rejection();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_article_rejections_no_analysis "
        "ON article_rejections;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_analysis_for_rejection();")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_article_analyses_no_rejection "
        "ON article_analyses;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_rejection_for_analysis();")
