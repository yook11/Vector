"""rename article_extractions / extraction_noises to curation naming.

PR-E.1: Stage 3 の責務が「構造抽出 (extraction)」から「選別 + 意味づけ
(curation)」へ移った実体に合わせ、DB table / FK column / 関連 constraint /
trigger / function を curation 命名へ揃える。Python 側 rename (#577, PR-E.0)
の続編。

本 migration が触るのは:
- table 2 個 (article_extractions → article_curations、
  extraction_noises → curation_noises)
- 依存する明示 named constraint 6 個 (両 table の UNIQUE / CHECK)
- 自動命名 FK 2 個 (両 table の article_id → articles.id)
- sequence 2 個 (両 table の PK SERIAL/BIGSERIAL)
- signal/noise 排他 trigger pair 2 個 + 対応 function 2 個
  (body 内 table 名を hardcode しているため drop+recreate)
- in_scope_assessments / out_of_scope_assessments の FK column
  ``extraction_id`` → ``curation_id`` (2 個) + 対応 FK / UNIQUE constraint 名
- assessment 相互排他 trigger pair 2 個 + 対応 function 2 個
  (body 内 column 名 ``extraction_id`` を hardcode しているため、column
  rename に合わせて drop+recreate。trigger / function 名自体は前例 t1 で
  確立済なので据え置きで body だけ新 column 名で再作成)

振る舞いと意味は不変: ``extracted_at`` 列名 / ``pipeline_events.payload``
JSONB 内の historical ``extraction_id`` key / ``outcome_code`` の
``"extracted"`` / ``"extracted_as_noise"`` は wire format として据え置く。

deploy 段取りは PR description の runbook を参照 (stop-the-world: 全 worker
+ API server 停止 → migrate → 新 image deploy → smoke test → resume)。

Revision ID: t2_curation_table_rename
Revises: aa3_pending_source_name_nn_fk
Create Date: 2026-05-21
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "t2_curation_table_rename"
down_revision: str | None = "aa3_pending_source_name_nn_fk"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


# ORM `__table_args__` で name= 明示している constraint の rename ペア。
_ARTICLE_CURATIONS_NAMED_CONSTRAINT_RENAMES: list[tuple[str, str]] = [
    (
        "uq_article_extractions_article_id",
        "uq_article_curations_article_id",
    ),
    (
        "ck_article_extractions_translated_title_not_empty",
        "ck_article_curations_translated_title_not_empty",
    ),
    (
        "ck_article_extractions_summary_not_empty",
        "ck_article_curations_summary_not_empty",
    ),
]

_CURATION_NOISES_NAMED_CONSTRAINT_RENAMES: list[tuple[str, str]] = [
    (
        "uq_extraction_noises_article_id",
        "uq_curation_noises_article_id",
    ),
    (
        "ck_extraction_noises_title_ja_not_empty",
        "ck_curation_noises_title_ja_not_empty",
    ),
    (
        "ck_extraction_noises_summary_ja_not_empty",
        "ck_curation_noises_summary_ja_not_empty",
    ),
]

# FK column rename 後の incoming FK / UNIQUE constraint name rename。
# in_scope_assessments / out_of_scope_assessments の extraction_id → curation_id
# rename に合わせて constraint 名も同期する (前例 t1 で named 化済み)。
_IN_SCOPE_ASSESSMENT_CONSTRAINT_RENAMES: list[tuple[str, str]] = [
    (
        "uq_in_scope_assessments_extraction_id",
        "uq_in_scope_assessments_curation_id",
    ),
    (
        "fk_in_scope_assessments_extraction_id",
        "fk_in_scope_assessments_curation_id",
    ),
]

_OUT_OF_SCOPE_ASSESSMENT_CONSTRAINT_RENAMES: list[tuple[str, str]] = [
    (
        "uq_out_of_scope_assessments_extraction_id",
        "uq_out_of_scope_assessments_curation_id",
    ),
    (
        "fk_out_of_scope_assessments_extraction_id",
        "fk_out_of_scope_assessments_curation_id",
    ),
]

# 自動命名 FK (article_id → articles.id) の新名。
# p1_add_extraction_noises / d6f7a8b9c0d1_create_article_extractions は
# sa.ForeignKey(...) のみ (name 引数なし) で生成しているため postgres が自動命名。
_ARTICLE_CURATIONS_FK_NEW_NAMES: dict[tuple[str, ...], str] = {
    ("article_id",): "fk_article_curations_article_id",
}

_CURATION_NOISES_FK_NEW_NAMES: dict[tuple[str, ...], str] = {
    ("article_id",): "fk_curation_noises_article_id",
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
    #    本番で長時間 lock が取れない場合は早期 fail させる。先例は t1。
    op.execute("SET lock_timeout = '5s';")

    # 1. trigger / function を先に DROP。
    #    - signal/noise pair: body 内に旧 table 名 (extraction_noises /
    #      article_extractions) を hardcode
    #    - assessment 相互排他 pair: body 内に旧 column 名 (extraction_id) を
    #      hardcode (PL/pgSQL は実行時 reparse のため column rename だけだと
    #      次回 INSERT で "column extraction_id does not exist" で fail する)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_extraction_noises_no_extraction "
        "ON extraction_noises;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_article_extractions_no_noise "
        "ON article_extractions;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_extraction_for_noise();")
    op.execute("DROP FUNCTION IF EXISTS enforce_no_noise_for_extraction();")

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

    # 2. table rename
    op.rename_table("article_extractions", "article_curations")
    op.rename_table("extraction_noises", "curation_noises")

    # 3. ORM 明示の constraint rename (article_curations)
    for old, new in _ARTICLE_CURATIONS_NAMED_CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE article_curations RENAME CONSTRAINT {old} TO {new};")

    # 4. ORM 明示の constraint rename (curation_noises)
    for old, new in _CURATION_NOISES_NAMED_CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE curation_noises RENAME CONSTRAINT {old} TO {new};")

    # 5. 自動命名 FK rename (両 table の article_id → articles.id)
    _rename_auto_named_fks("article_curations", _ARTICLE_CURATIONS_FK_NEW_NAMES)
    _rename_auto_named_fks("curation_noises", _CURATION_NOISES_FK_NEW_NAMES)

    # 6. sequence rename (SERIAL / BIGSERIAL で自動生成された PK sequence)
    op.execute(
        "ALTER SEQUENCE article_extractions_id_seq RENAME TO article_curations_id_seq;"
    )
    op.execute(
        "ALTER SEQUENCE extraction_noises_id_seq RENAME TO curation_noises_id_seq;"
    )

    # 7. FK column rename (in_scope_assessments / out_of_scope_assessments の
    #    extraction_id → curation_id)
    op.alter_column(
        "in_scope_assessments",
        "extraction_id",
        new_column_name="curation_id",
    )
    op.alter_column(
        "out_of_scope_assessments",
        "extraction_id",
        new_column_name="curation_id",
    )

    # 8. incoming FK / UNIQUE constraint rename (column rename に合わせて
    #    constraint 名も同期、前例 t1 で named 化済みのものを再 rename)
    for old, new in _IN_SCOPE_ASSESSMENT_CONSTRAINT_RENAMES:
        op.execute(
            f"ALTER TABLE in_scope_assessments RENAME CONSTRAINT {old} TO {new};"
        )
    for old, new in _OUT_OF_SCOPE_ASSESSMENT_CONSTRAINT_RENAMES:
        op.execute(
            f"ALTER TABLE out_of_scope_assessments RENAME CONSTRAINT {old} TO {new};"
        )

    # 9. trigger / function を新名で CREATE (body 内 table 名 / column 名も新)。
    #    signal/noise 排他 pair。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_curation_noise_for_curation()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM curation_noises
                WHERE article_id = NEW.article_id
            ) THEN
                RAISE EXCEPTION
                    'article % already has a curation_noise', NEW.article_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_article_curations_no_curation_noise
        BEFORE INSERT OR UPDATE ON article_curations
        FOR EACH ROW EXECUTE FUNCTION enforce_no_curation_noise_for_curation();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_curation_for_curation_noise()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM article_curations
                WHERE article_id = NEW.article_id
            ) THEN
                RAISE EXCEPTION 'article % already has a curation', NEW.article_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_curation_noises_no_curation
        BEFORE INSERT OR UPDATE ON curation_noises
        FOR EACH ROW EXECUTE FUNCTION enforce_no_curation_for_curation_noise();
        """
    )

    # 10. assessment 相互排他 pair (trigger / function 名は据え置き、body 内
    #     column 名のみ curation_id へ更新)。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_out_of_scope_for_in_scope()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM out_of_scope_assessments
                WHERE curation_id = NEW.curation_id
            ) THEN
                RAISE EXCEPTION
                    'curation % already has an out_of_scope assessment',
                    NEW.curation_id
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
                WHERE curation_id = NEW.curation_id
            ) THEN
                RAISE EXCEPTION
                    'curation % already has an in_scope assessment',
                    NEW.curation_id
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

    # 完全対称: trigger / function drop → constraint reverse rename → 自動命名
    # FK drop → sequence reverse rename → column reverse rename → table reverse
    # rename → 自動命名 FK 再作成 → 旧 trigger / function 復元

    # 1. trigger / function を drop
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

    op.execute(
        "DROP TRIGGER IF EXISTS trg_curation_noises_no_curation ON curation_noises;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_article_curations_no_curation_noise "
        "ON article_curations;"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_no_curation_for_curation_noise();")
    op.execute("DROP FUNCTION IF EXISTS enforce_no_curation_noise_for_curation();")

    # 2. incoming FK / UNIQUE constraint reverse rename
    for old, new in _OUT_OF_SCOPE_ASSESSMENT_CONSTRAINT_RENAMES:
        op.execute(
            f"ALTER TABLE out_of_scope_assessments RENAME CONSTRAINT {new} TO {old};"
        )
    for old, new in _IN_SCOPE_ASSESSMENT_CONSTRAINT_RENAMES:
        op.execute(
            f"ALTER TABLE in_scope_assessments RENAME CONSTRAINT {new} TO {old};"
        )

    # 3. FK column reverse rename
    op.alter_column(
        "out_of_scope_assessments",
        "curation_id",
        new_column_name="extraction_id",
    )
    op.alter_column(
        "in_scope_assessments",
        "curation_id",
        new_column_name="extraction_id",
    )

    # 4. sequence reverse rename
    op.execute(
        "ALTER SEQUENCE curation_noises_id_seq RENAME TO extraction_noises_id_seq;"
    )
    op.execute(
        "ALTER SEQUENCE article_curations_id_seq RENAME TO article_extractions_id_seq;"
    )

    # 5. 自動命名 FK drop (旧自動名は環境依存で復元できないため、DROP + ADD)
    _drop_auto_named_fks("curation_noises", _CURATION_NOISES_FK_NEW_NAMES)
    _drop_auto_named_fks("article_curations", _ARTICLE_CURATIONS_FK_NEW_NAMES)

    # 6. ORM 明示 constraint reverse rename
    for old, new in _CURATION_NOISES_NAMED_CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE curation_noises RENAME CONSTRAINT {new} TO {old};")
    for old, new in _ARTICLE_CURATIONS_NAMED_CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE article_curations RENAME CONSTRAINT {new} TO {old};")

    # 7. table reverse rename
    op.rename_table("curation_noises", "extraction_noises")
    op.rename_table("article_curations", "article_extractions")

    # 8. 自動命名 FK 再作成 (postgres 自動命名でいい — upgrade 時に inspector で
    #    取得する設計)
    op.execute(
        "ALTER TABLE article_extractions "
        "ADD FOREIGN KEY (article_id) "
        "REFERENCES articles(id) ON DELETE CASCADE;"
    )
    op.execute(
        "ALTER TABLE extraction_noises "
        "ADD FOREIGN KEY (article_id) "
        "REFERENCES articles(id) ON DELETE CASCADE;"
    )

    # 9. 旧 trigger / function 復元 (p1_add_extraction_noises + t1 の upgrade と
    #    同じ SQL)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_noise_for_extraction()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM extraction_noises
                WHERE article_id = NEW.article_id
            ) THEN
                RAISE EXCEPTION
                    'article % already has an extraction_noise', NEW.article_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_article_extractions_no_noise
        BEFORE INSERT OR UPDATE ON article_extractions
        FOR EACH ROW EXECUTE FUNCTION enforce_no_noise_for_extraction();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_no_extraction_for_noise()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM article_extractions
                WHERE article_id = NEW.article_id
            ) THEN
                RAISE EXCEPTION 'article % already has an extraction', NEW.article_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_extraction_noises_no_extraction
        BEFORE INSERT OR UPDATE ON extraction_noises
        FOR EACH ROW EXECUTE FUNCTION enforce_no_extraction_for_noise();
        """
    )

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
