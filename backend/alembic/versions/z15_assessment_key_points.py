"""rename assessment JSONB column events -> key_points (inner description -> content).

Stage 4 の出力アンカーを「起こった出来事 (events[].description)」から
「記事の重要な情報 (key_points[].content)」へ作り替える本番データ移行:

- ``in_scope_assessments`` / ``out_of_scope_assessments`` の JSONB 列
  ``events`` → ``key_points`` に rename
- 各配列要素の内側キー ``description`` → ``content`` に rename (値は保持)
- ``pipeline_events.outcome_code`` の旧 assessment defect 4 値を新値へ rename
  (``AssessmentResponseDefect`` の value 変更を過去の監査行にも反映する)

順序は両テーブルで「内側キー変換 (旧列名 ``events`` のまま) → 列 rename」。
配列順は ``WITH ORDINALITY`` で保持。NULL / ``[]`` / 非 object 要素 /
``description`` 欠落要素は ``CASE`` と ``HAVING`` で素通しし、再実行しても
``(elem - 'description' - 'content')`` で description/content 両在の中間状態から
収束する (冪等)。alembic は migration を transaction で包むため crash-safe。

``outcome_code`` は索引付きの集計キーで、value 変更を新規行だけに反映すると
過去行と新行で同一故障が別語彙に分裂する。``assessment_response_*`` は名前空間で
全域一意なため stage で絞らず値一致のみで rewrite する (前例
z5_curation_outcome_rename と同型、ただし ASSESSMENT / BACKFILL_ASSESS 両 stage を
拾うよう stage 非依存にする)。

deploy 段取りは stop-the-world (全 process 停止 → queue drain → migrate →
新 image deploy → resume)。``events`` は稼働中 ``AssessmentRepository`` が書き込む
live 列であり、列名変更は本 PR の writer 変更 (key_points への切替) と同時に行う。
rolling deploy で「新列適用後に旧 worker が ``events`` へ INSERT」すると
UndefinedColumn になるため、旧/新 worker の混在を避ける。

Revision ID: z15_assessment_key_points
Revises: z14_grant_collect_role
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z15_assessment_key_points"
down_revision: str | None = "z14_grant_collect_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: UPDATE + alter_column(new_column_name=) を含むため contract。
MIGRATION_KIND = "contract"

_TABLES: tuple[str, ...] = ("in_scope_assessments", "out_of_scope_assessments")

# AssessmentResponseDefect の value 変更 (旧 → 新)。outcome_code として永続化された
# 過去の監査行をこの対で書き換える。値は名前空間で全域一意のため stage 非依存に絞れる。
_OUTCOME_CODE_RENAMES: tuple[tuple[str, str], ...] = (
    (
        "assessment_response_events_key_missing",
        "assessment_response_key_points_key_missing",
    ),
    (
        "assessment_response_events_wrong_type",
        "assessment_response_key_points_wrong_type",
    ),
    ("assessment_response_events_too_many", "assessment_response_key_points_too_many"),
    ("assessment_response_event_invalid", "assessment_response_key_point_invalid"),
)


def _rewrite_outcome_code_sql(*, old: str, new: str) -> str:
    """``pipeline_events.outcome_code`` を ``old`` → ``new`` に書き換える SQL。

    値一致のみで絞る (再実行で 0 行一致 → 冪等)。``outcome_code`` に CHECK は無く、
    rolling 中に新旧両値が並んでも IntegrityError は起きない。
    """
    # old / new は module 内 literal (外部入力なし)、S608 は無害。
    return (
        "UPDATE pipeline_events"  # noqa: S608
        f" SET outcome_code = '{new}'"
        f" WHERE outcome_code = '{old}'"
    )


def _rename_inner_key_sql(table: str, *, from_key: str, to_key: str) -> str:
    """JSONB 配列列 ``events`` の各要素で ``from_key`` → ``to_key`` に rename する SQL。

    ``events`` という列名はデータ変換時点での物理列名 (upgrade では rename 前、
    downgrade では rename を戻した後) で常に有効。配列順は ``WITH ORDINALITY``
    で保持。``from_key`` を持つ object 要素のみ書き換え、既に ``to_key`` 化済みの
    要素は ``HAVING`` で対象外になり冪等。

    ``jsonb_typeof(elem) = 'object'`` ガードは非 object 要素を CASE/HAVING の両方で
    除外する。``elem ? '{from_key}'`` は文字列スカラ要素 (例 ``"description"``) にも
    true を返し、後段の ``elem - '{from_key}'`` が "cannot delete from scalar" で
    migration tx 全体を abort させるため、object に限定して防ぐ (破損/手書き行対策)。
    """
    # table / from_key / to_key は module 内 literal (外部入力なし)、S608 は無害。
    return (
        f"UPDATE {table} a SET events = sub.kp FROM ("  # noqa: S608
        " SELECT t.id, jsonb_agg("
        f"  CASE WHEN jsonb_typeof(elem) = 'object' AND elem ? '{from_key}'"
        f"   THEN (elem - '{from_key}' - '{to_key}')"
        f"        || jsonb_build_object('{to_key}', elem->'{from_key}')"
        "   ELSE elem END"
        "  ORDER BY ord) AS kp"
        f" FROM {table} t,"
        " LATERAL jsonb_array_elements(t.events)"
        " WITH ORDINALITY AS arr(elem, ord)"
        " WHERE jsonb_typeof(t.events) = 'array' AND t.events <> '[]'::jsonb"
        " GROUP BY t.id"
        f" HAVING bool_or(jsonb_typeof(elem) = 'object' AND elem ? '{from_key}')"
        ") sub WHERE a.id = sub.id"
    )


def upgrade() -> None:
    # lock_timeout: deploy window 内でも他 tx が長く lock を握る事故を予防 (5s)。
    op.execute("SET lock_timeout = '5s';")

    for table in _TABLES:
        # 内側キー変換は列名 events のまま実施 (rename は変換後)。
        op.execute(
            _rename_inner_key_sql(table, from_key="description", to_key="content")
        )
        op.alter_column(table, "events", new_column_name="key_points")

    # 監査 outcome_code の旧 defect 値を新値へ揃える (過去行も新語彙に統一)。
    for old, new in _OUTCOME_CODE_RENAMES:
        op.execute(_rewrite_outcome_code_sql(old=old, new=new))


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    for old, new in _OUTCOME_CODE_RENAMES:
        op.execute(_rewrite_outcome_code_sql(old=new, new=old))

    for table in _TABLES:
        # 列名を戻してから内側キーを逆変換 (events 列前提に揃える)。
        op.alter_column(table, "key_points", new_column_name="events")
        op.execute(
            _rename_inner_key_sql(table, from_key="content", to_key="description")
        )
