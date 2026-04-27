"""normalize article_entity names with NFKC and whitespace collapse

EntityName VO に追加された NFKC + 連続空白統合 + 前後空白除去を
既存行に backfill する (PR #165 の VO 改修と対応)。
新規 INSERT は VO で保証されるが、過去データには未整形が混在しうる。

責務:
- 既存 article_entities.name を VO と同一規則で整形して上書き
- 完了後、未整形行が 0 であることを検証
- 整形結果が 1-200 文字に収まらない / 空になるケースは、ローカル監査で
  0 件と確認済みだがフェイルファストで raise する (隠さない方針:
  feedback_failure_visibility.md)

ローカル監査結果 (2026-04-27):
  total=6874 / will_change=17 / over_200_after_normalize=0
  主な変化: 全角括弧→半角、上付き数字→通常数字、商標記号 (™→TM) 等

NFKC は Postgres にネイティブ関数がないため Python 側で実施する。
unique 制約は (article_extraction_id, name, type) に存在しないため、
重複の取り扱いは不要 (後段の集計で match_key により集約する)。

Revision ID: b7eadad7f3cc
Revises: 4592638692be
Create Date: 2026-04-27 11:03:43.305470

"""

import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7eadad7f3cc"
down_revision: str | None = "4592638692be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# VO (app/analysis/domain/value_objects/entity.py) と同一規則。
_WHITESPACE_RUN = re.compile(r"\s+")
_NAME_MAX_LENGTH = 200


def _normalize(name: str) -> str:
    return _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFKC", name)).strip()


def upgrade() -> None:
    bind = op.get_bind()

    rows = bind.execute(sa.text("SELECT id, name FROM article_entities")).fetchall()
    print(f"[migration] baseline: total={len(rows)}")

    updates: list[tuple[int, str]] = []
    invalid: list[tuple[int, str, str]] = []
    for row in rows:
        normalized = _normalize(row.name)
        if normalized == row.name:
            continue
        if not normalized or len(normalized) > _NAME_MAX_LENGTH:
            invalid.append((row.id, row.name, normalized))
            continue
        updates.append((row.id, normalized))

    if invalid:
        sample = invalid[:5]
        raise RuntimeError(
            f"Cannot normalize {len(invalid)} rows (would become empty or "
            f">{_NAME_MAX_LENGTH} chars). Sample: {sample!r}. "
            "Manual cleanup required before retrying this migration."
        )

    print(f"[migration] will_update={len(updates)}")
    for entity_id, normalized in updates:
        bind.execute(
            sa.text("UPDATE article_entities SET name = :n WHERE id = :i"),
            {"n": normalized, "i": entity_id},
        )

    # 検証: 全行を再フェッチし、整形済みでない行が 0 であることを確認。
    verify_rows = bind.execute(
        sa.text("SELECT id, name FROM article_entities")
    ).fetchall()
    remaining = [r.id for r in verify_rows if r.name != _normalize(r.name)]
    if remaining:
        raise RuntimeError(
            f"Backfill incomplete: {len(remaining)} rows still need "
            f"normalization. Sample IDs: {remaining[:10]}"
        )
    print(f"[migration] verified: all {len(verify_rows)} rows normalized")


def downgrade() -> None:
    """Downgrade is unsupported: original (un-normalized) strings are lost."""
    raise NotImplementedError(
        "Downgrade is not supported: original article_entities.name values "
        "before NFKC + whitespace normalization are not recoverable."
    )
