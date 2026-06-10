"""remap weekly_briefings.key_articles の旧形 article_id を assessment_id へ統一する。

briefing key_articles の id 空間移行 (parallel change) の Phase B data migration:

- 旧形要素 ``{"article_id": <Article.id>, ...}`` を公開 /news id 空間の新形
  ``{"assessment_id": <InScopeAssessment.id>, ...}`` へ書き換える
  (``article_curations.article_id`` → ``in_scope_assessments.curation_id`` の
  1:1 join、curation_id UNIQUE)。
- Phase A (PR #787) で writer は新形書込・reader は両形対応済みのため、本
  migration は稼働中でも無停止で適用できる (stop-the-world 不要)。適用後に
  旧形 reader 経路を削除する同 PR のコードを deploy する。
- 事前ガード: 対応 assessment が引けない旧形 article_id が 1 件でもあれば
  raise して abort。CASE の相関 subquery が NULL を返して JSON null が混入
  したり、join 落ちで配列が縮む silent drop を構造的に防ぐ。
- 配列順は ``WITH ORDINALITY`` で保持。``article_id`` キーを持つ object 要素
  のみ書き換え、``HAVING`` で対象行を絞るため再実行は no-op (冪等)。

Revision ID: w4_remap_key_article_ids
Revises: w3_merge_briefing_trends
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "w4_remap_key_article_ids"
down_revision: str | None = "w3_merge_briefing_trends"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: data UPDATE を含むため contract。
MIGRATION_KIND = "contract"

# elem (jsonb_array_elements の要素) から移行先 id を引く相関 subquery。
# EXISTS ガードと remap 本体で同じものを使い、判定と書き換えの対象を一致させる。
_TO_ASSESSMENT_ID = (
    "SELECT isa.id FROM article_curations ac"
    " JOIN in_scope_assessments isa ON isa.curation_id = ac.id"
    " WHERE ac.article_id = (elem->>'article_id')::int"
)
_TO_ARTICLE_ID = (
    "SELECT ac.article_id FROM in_scope_assessments isa"
    " JOIN article_curations ac ON ac.id = isa.curation_id"
    " WHERE isa.id = (elem->>'assessment_id')::int"
)


def _unresolved_count_sql(*, from_key: str, subquery: str) -> str:
    """``from_key`` 要素のうち移行先 id を解決できない件数を数える SQL。"""
    # from_key / subquery は module 内 literal (外部入力なし)、S608 は無害。
    return (
        "SELECT count(*) FROM weekly_briefings wb,"  # noqa: S608
        " LATERAL jsonb_array_elements(wb.key_articles) AS arr(elem)"
        " WHERE jsonb_typeof(wb.key_articles) = 'array'"
        f" AND jsonb_typeof(elem) = 'object' AND elem ? '{from_key}'"
        f" AND NOT EXISTS ({subquery})"
    )


def _remap_sql(*, from_key: str, to_key: str, subquery: str) -> str:
    """JSONB 配列の各 ``from_key`` 要素を ``to_key`` + 解決済み id へ書き換える SQL。

    配列順は ``WITH ORDINALITY`` で保持。``from_key`` を持つ object 要素のみ
    書き換え、既に変換済みの行は ``HAVING`` で対象外になり冪等。
    ``jsonb_typeof(elem) = 'object'`` ガードは非 object 要素 (破損/手書き行) の
    ``elem - key`` が scalar で abort するのを防ぐ (z15 と同じ防御)。
    """
    # from_key / to_key / subquery は module 内 literal (外部入力なし)、S608 は無害。
    return (
        "UPDATE weekly_briefings a SET key_articles = sub.ka FROM ("  # noqa: S608
        " SELECT t.id, jsonb_agg("
        f"  CASE WHEN jsonb_typeof(elem) = 'object' AND elem ? '{from_key}'"
        f"   THEN (elem - '{from_key}')"
        f"        || jsonb_build_object('{to_key}', ({subquery}))"
        "   ELSE elem END"
        "  ORDER BY ord) AS ka"
        " FROM weekly_briefings t,"
        " LATERAL jsonb_array_elements(t.key_articles)"
        " WITH ORDINALITY AS arr(elem, ord)"
        " WHERE jsonb_typeof(t.key_articles) = 'array'"
        " AND t.key_articles <> '[]'::jsonb"
        " GROUP BY t.id"
        f" HAVING bool_or(jsonb_typeof(elem) = 'object' AND elem ? '{from_key}')"
        ") sub WHERE a.id = sub.id"
    )


def _guard_unresolved(*, from_key: str, subquery: str) -> None:
    """移行先 id を解決できない要素があれば migration を abort する。"""
    unresolved = (
        op.get_bind()
        .execute(text(_unresolved_count_sql(from_key=from_key, subquery=subquery)))
        .scalar_one()
    )
    if unresolved:
        raise RuntimeError(
            f"key_articles remap aborted: {unresolved} element(s) with "
            f"'{from_key}' have no resolvable counterpart id"
        )


def upgrade() -> None:
    # lock_timeout: 他 tx が長く lock を握る事故を予防 (5s)。
    op.execute("SET lock_timeout = '5s';")
    _guard_unresolved(from_key="article_id", subquery=_TO_ASSESSMENT_ID)
    op.execute(
        _remap_sql(
            from_key="article_id",
            to_key="assessment_id",
            subquery=_TO_ASSESSMENT_ID,
        )
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    _guard_unresolved(from_key="assessment_id", subquery=_TO_ARTICLE_ID)
    op.execute(
        _remap_sql(
            from_key="assessment_id",
            to_key="article_id",
            subquery=_TO_ARTICLE_ID,
        )
    )
