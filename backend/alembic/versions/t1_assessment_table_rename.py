"""rename article_analyses / article_rejections to assessment naming.

PR3.5-d.1: PR3.5-d.0 (Python rename, #427) の続編。Domain Entity 名
(InScopeAssessment / OutOfScopeAssessment) と DB table 名を一致させる。

本 migration が触るのは:
- table 名 2 個 (article_analyses → in_scope_assessments、
  article_rejections → out_of_scope_assessments)
- 依存する named constraint 11 個、自動命名 FK 3 個、Index 2 個、
  排他 trigger 2 個、関連 function 2 個

振る舞いと column 名は不変 (column ``article_analysis_id`` は API/DB 互換のため
据え置き)。

deploy 段取りは PR description の runbook を参照
(全 process 停止 → migrate → 新 image deploy → resume の stop-the-world)。

Revision ID: t1_assessment_table_rename
Revises: r1_pe_category_code
Create Date: 2026-05-09
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "t1_assessment_table_rename"
down_revision: str | None = "r1_pe_category_code"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


# ORM `__table_args__` で name= 明示している constraint / index の rename ペア。
_IN_SCOPE_NAMED_CONSTRAINT_RENAMES: list[tuple[str, str]] = [
    (
        "uq_article_analyses_extraction_id",
        "uq_in_scope_assessments_extraction_id",
    ),
    (
        "ck_article_analyses_translated_title_not_empty",
        "ck_in_scope_assessments_translated_title_not_empty",
    ),
    (
        "ck_article_analyses_summary_not_empty",
        "ck_in_scope_assessments_summary_not_empty",
    ),
    (
        "ck_article_analyses_ai_model_not_empty",
        "ck_in_scope_assessments_ai_model_not_empty",
    ),
    (
        "ck_article_analyses_investor_take_not_empty",
        "ck_in_scope_assessments_investor_take_not_empty",
    ),
    (
        "ck_article_analyses_topic_not_empty",
        "ck_in_scope_assessments_topic_not_empty",
    ),
    (
        "ck_article_analyses_topic_format",
        "ck_in_scope_assessments_topic_format",
    ),
    (
        "ck_article_analyses_embedding_consistency",
        "ck_in_scope_assessments_embedding_consistency",
    ),
]

_OUT_OF_SCOPE_NAMED_CONSTRAINT_RENAMES: list[tuple[str, str]] = [
    (
        "uq_article_rejections_extraction_id",
        "uq_out_of_scope_assessments_extraction_id",
    ),
    (
        "ck_article_rejections_investor_take_not_empty",
        "ck_out_of_scope_assessments_investor_take_not_empty",
    ),
    (
        "ck_article_rejections_ai_model_not_empty",
        "ck_out_of_scope_assessments_ai_model_not_empty",
    ),
]

# 自動命名 FK の新名 (constrained_columns tuple → new_name)。
# d2e3f4a5b6c7 / d7a8b9c0d1e2 / 4d16d9b326a0 で sa.ForeignKey(...) のみ
# (name 引数なし) で生成しているため postgres が自動命名している。
_IN_SCOPE_FK_NEW_NAMES: dict[tuple[str, ...], str] = {
    ("extraction_id",): "fk_in_scope_assessments_extraction_id",
    ("category_id",): "fk_in_scope_assessments_category_id",
}

_OUT_OF_SCOPE_FK_NEW_NAMES: dict[tuple[str, ...], str] = {
    ("extraction_id",): "fk_out_of_scope_assessments_extraction_id",
}


def _rename_auto_named_fks(table: str, fk_map: dict[tuple[str, ...], str]) -> None:
    """自動命名 FK を inspector で実名取得し、新名に RENAME CONSTRAINT する。"""
    bind = op.get_bind()
    insp = inspect(bind)
    for fk in insp.get_foreign_keys(table):
        cols = tuple(fk.get("constrained_columns") or [])
        new_name = fk_map.get(cols)
        old_name = fk.get("name")
        if new_name and old_name and old_name != new_name:
            op.execute(
                f"ALTER TABLE {table} RENAME CONSTRAINT {old_name} TO {new_name};"
            )


def _drop_auto_named_fks(table: str, fk_map: dict[tuple[str, ...], str]) -> None:
    """対象 column の自動命名 FK を実名取得して DROP する (downgrade 用)。"""
    bind = op.get_bind()
    insp = inspect(bind)
    for fk in insp.get_foreign_keys(table):
        cols = tuple(fk.get("constrained_columns") or [])
        cur_name = fk.get("name")
        if cols in fk_map and cur_name:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {cur_name};")


def upgrade() -> None:
    # 0. lock_timeout: rename は metadata 操作だが table lock を取るため、
    #    本番で長時間 lock が取れない場合は早期 fail させる。先例は 4d16d9b326a0。
    op.execute("SET lock_timeout = '5s';")

    # 1. trigger / function を先に DROP (function body 内 SQL に旧 table 名が
    #    hardcode されているため、rename ではなく drop+recreate で扱う)。
    op.execute(
        "DROP TRIGGER IF EXISTS trg_article_rejections_no_analysis "
        "ON article_rejections;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_article_analyses_no_rejection ON article_analyses;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_analysis_for_rejection();")
    op.execute("DROP FUNCTION IF EXISTS enforce_no_rejection_for_analysis();")

    # 2. table rename
    op.rename_table("article_analyses", "in_scope_assessments")
    op.rename_table("article_rejections", "out_of_scope_assessments")

    # 3. ORM 明示の constraint rename (in_scope)
    for old, new in _IN_SCOPE_NAMED_CONSTRAINT_RENAMES:
        op.execute(
            f"ALTER TABLE in_scope_assessments RENAME CONSTRAINT {old} TO {new};"
        )

    # 4. 自動命名 FK rename (in_scope の extraction_id / category_id)
    _rename_auto_named_fks("in_scope_assessments", _IN_SCOPE_FK_NEW_NAMES)

    # 5. index rename (in_scope)
    op.execute(
        "ALTER INDEX ix_article_analyses_category_id_analyzed_at "
        "RENAME TO ix_in_scope_assessments_category_id_analyzed_at;"
    )
    op.execute(
        "ALTER INDEX idx_article_analyses_embedding "
        "RENAME TO idx_in_scope_assessments_embedding;"
    )

    # 6. ORM 明示の constraint rename (out_of_scope)
    for old, new in _OUT_OF_SCOPE_NAMED_CONSTRAINT_RENAMES:
        op.execute(
            f"ALTER TABLE out_of_scope_assessments RENAME CONSTRAINT {old} TO {new};"
        )

    # 7. 自動命名 FK rename (out_of_scope の extraction_id)
    _rename_auto_named_fks("out_of_scope_assessments", _OUT_OF_SCOPE_FK_NEW_NAMES)

    # 8. watchlist_entries の FK target table は postgres が rename_table 時に
    #    OID 経由で自動追従するため何もしない (constraint 名は据え置き、
    #    column 名 article_analysis_id 据え置きと整合)。

    # 9. trigger function を新名で CREATE (body 内 table 名も新名)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_out_of_scope_for_in_scope()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM out_of_scope_assessments
                WHERE extraction_id = NEW.extraction_id
            ) THEN
                RAISE EXCEPTION
                    'extraction % already has an out_of_scope assessment',
                    NEW.extraction_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_in_scope_assessments_no_out_of_scope
        BEFORE INSERT OR UPDATE ON in_scope_assessments
        FOR EACH ROW EXECUTE FUNCTION enforce_no_out_of_scope_for_in_scope();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_in_scope_for_out_of_scope()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM in_scope_assessments
                WHERE extraction_id = NEW.extraction_id
            ) THEN
                RAISE EXCEPTION
                    'extraction % already has an in_scope assessment',
                    NEW.extraction_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_out_of_scope_assessments_no_in_scope
        BEFORE INSERT OR UPDATE ON out_of_scope_assessments
        FOR EACH ROW EXECUTE FUNCTION enforce_no_in_scope_for_out_of_scope();
        """
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # 完全対称: trigger drop → 自動命名 FK drop → constraint / index reverse
    # rename → table reverse rename → 自動命名 FK 再作成 → 旧 trigger /
    # function 復元
    op.execute(
        "DROP TRIGGER IF EXISTS trg_out_of_scope_assessments_no_in_scope "
        "ON out_of_scope_assessments;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_in_scope_assessments_no_out_of_scope "
        "ON in_scope_assessments;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_in_scope_for_out_of_scope();")
    op.execute("DROP FUNCTION IF EXISTS enforce_no_out_of_scope_for_in_scope();")

    # 自動命名 FK の新名を DROP (旧自動名は環境依存で復元できないため、
    # rename ではなく DROP + ADD で再作成する)。
    _drop_auto_named_fks("in_scope_assessments", _IN_SCOPE_FK_NEW_NAMES)
    _drop_auto_named_fks("out_of_scope_assessments", _OUT_OF_SCOPE_FK_NEW_NAMES)

    # ORM 明示 constraint を旧名に戻す (out_of_scope を先、index、in_scope の順
    # は upgrade と逆)
    for old, new in _OUT_OF_SCOPE_NAMED_CONSTRAINT_RENAMES:
        op.execute(
            f"ALTER TABLE out_of_scope_assessments RENAME CONSTRAINT {new} TO {old};"
        )
    op.execute(
        "ALTER INDEX idx_in_scope_assessments_embedding "
        "RENAME TO idx_article_analyses_embedding;"
    )
    op.execute(
        "ALTER INDEX ix_in_scope_assessments_category_id_analyzed_at "
        "RENAME TO ix_article_analyses_category_id_analyzed_at;"
    )
    for old, new in _IN_SCOPE_NAMED_CONSTRAINT_RENAMES:
        op.execute(
            f"ALTER TABLE in_scope_assessments RENAME CONSTRAINT {new} TO {old};"
        )

    # table rename 戻し
    op.rename_table("in_scope_assessments", "article_analyses")
    op.rename_table("out_of_scope_assessments", "article_rejections")

    # FK 再作成 (postgres 自動命名でいい — upgrade 時に inspector で取得する設計)
    op.execute(
        "ALTER TABLE article_analyses "
        "ADD FOREIGN KEY (extraction_id) "
        "REFERENCES article_extractions(id) ON DELETE CASCADE;"
    )
    op.execute(
        "ALTER TABLE article_analyses "
        "ADD FOREIGN KEY (category_id) "
        "REFERENCES categories(id) ON DELETE RESTRICT;"
    )
    op.execute(
        "ALTER TABLE article_rejections "
        "ADD FOREIGN KEY (extraction_id) "
        "REFERENCES article_extractions(id) ON DELETE CASCADE;"
    )

    # 旧 function / trigger を d5e6f7a8b9ca の upgrade と同じ SQL で復元
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_rejection_for_analysis()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM article_rejections
                WHERE extraction_id = NEW.extraction_id
            ) THEN
                RAISE EXCEPTION 'extraction % already has a rejection',
                    NEW.extraction_id
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
                RAISE EXCEPTION 'extraction % already has an analysis',
                    NEW.extraction_id
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
