"""rename pending_html_articles to incomplete_articles.

#605 の続編。Stage 1 writer (PendingHtmlEnqueue → IncompleteArticleRepository) の
先行改名に続き、本丸である table / model を改名する。
旧名 ``pending_html_articles`` は「HTML 取得待ち」という機構を語っていたが、
ドメイン状態 (本文未取得の未完成記事) を語る ``incomplete_articles`` に揃える。
column / 振る舞いは不変。

本 migration が触るのは:
- table 名 1 個 (pending_html_articles → incomplete_articles)
- ORM 明示の named constraint 7 個、Index 2 個
- 自動命名 FK 1 個 (source_id 単独)、PK constraint、id sequence
  (これらは create_all 出力 (= test schema) に合わせ prod も改名し parity を保つ)

trigger は無いため drop/recreate 段は不要 (テンプレ t1 より単純)。

deploy 段取りは t1 と同様、rename は table lock を取るため lock_timeout で早期 fail。

Revision ID: u1_incomplete_articles_rename
Revises: t2_curation_table_rename
Create Date: 2026-05-24
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "u1_incomplete_articles_rename"
down_revision: str | None = "t2_curation_table_rename"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


# ORM `__table_args__` で name= 明示している constraint / index の rename ペア。
_NAMED_CONSTRAINT_RENAMES: list[tuple[str, str]] = [
    ("uq_pending_html_articles_url", "uq_incomplete_articles_url"),
    (
        "fk_pending_html_articles_source_id_name",
        "fk_incomplete_articles_source_id_name",
    ),
    ("ck_pending_html_articles_url_scheme", "ck_incomplete_articles_url_scheme"),
    ("ck_pending_html_articles_status", "ck_incomplete_articles_status"),
    (
        "ck_pending_html_articles_state_consistency",
        "ck_incomplete_articles_state_consistency",
    ),
    (
        "ck_pending_html_articles_ready_required",
        "ck_incomplete_articles_ready_required",
    ),
    (
        "ck_pending_html_articles_attempt_nonneg",
        "ck_incomplete_articles_attempt_nonneg",
    ),
]

_INDEX_RENAMES: list[tuple[str, str]] = [
    ("ix_pending_html_articles_ready", "ix_incomplete_articles_ready"),
    ("ix_pending_html_articles_expired_lease", "ix_incomplete_articles_expired_lease"),
]

# 自動命名 FK (source_id 単独、model では column 上の sa.ForeignKey のみで name 無し)。
# 旧名は環境依存のため inspector で実名取得して rename する。
_FK_NEW_NAMES: dict[tuple[str, ...], str] = {
    ("source_id",): "incomplete_articles_source_id_fkey",
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


def _rename_auto_named_fks_reverse(
    table: str, fk_map: dict[tuple[str, ...], str], new_to_old: dict[str, str]
) -> None:
    """新名 FK を旧名 (postgres 既定) に戻す (downgrade 用)。"""
    bind = op.get_bind()
    insp = inspect(bind)
    for fk in insp.get_foreign_keys(table):
        cols = tuple(fk.get("constrained_columns") or [])
        cur_name = fk.get("name")
        if cols in fk_map and cur_name and cur_name in new_to_old:
            op.execute(
                f"ALTER TABLE {table} RENAME CONSTRAINT {cur_name} "
                f"TO {new_to_old[cur_name]};"
            )


def upgrade() -> None:
    # 0. rename は metadata 操作だが table lock を取るため、本番で長時間 lock が
    #    取れない場合は早期 fail させる (先例 t1)。
    op.execute("SET lock_timeout = '5s';")

    # 1. table rename
    op.rename_table("pending_html_articles", "incomplete_articles")

    # 2. ORM 明示の constraint rename
    for old, new in _NAMED_CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE incomplete_articles RENAME CONSTRAINT {old} TO {new};")

    # 3. index rename
    for old, new in _INDEX_RENAMES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new};")

    # 4. 自動命名 FK (source_id 単独) を新名へ
    _rename_auto_named_fks("incomplete_articles", _FK_NEW_NAMES)

    # 5. parity rename (prod schema を create_all 出力に一致させる)。
    #    rename_table は PK constraint / sequence を追従改名しないため明示で行う。
    op.execute(
        "ALTER TABLE incomplete_articles "
        "RENAME CONSTRAINT pending_html_articles_pkey TO incomplete_articles_pkey;"
    )
    op.execute(
        "ALTER SEQUENCE pending_html_articles_id_seq "
        "RENAME TO incomplete_articles_id_seq;"
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # 完全対称に逆操作 (parity → 自動命名 FK → index → constraint → table)。
    op.execute(
        "ALTER SEQUENCE incomplete_articles_id_seq "
        "RENAME TO pending_html_articles_id_seq;"
    )
    op.execute(
        "ALTER TABLE incomplete_articles "
        "RENAME CONSTRAINT incomplete_articles_pkey TO pending_html_articles_pkey;"
    )

    _rename_auto_named_fks_reverse(
        "incomplete_articles",
        _FK_NEW_NAMES,
        {"incomplete_articles_source_id_fkey": "pending_html_articles_source_id_fkey"},
    )

    for old, new in _INDEX_RENAMES:
        op.execute(f"ALTER INDEX {new} RENAME TO {old};")

    for old, new in _NAMED_CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE incomplete_articles RENAME CONSTRAINT {new} TO {old};")

    op.rename_table("incomplete_articles", "pending_html_articles")
