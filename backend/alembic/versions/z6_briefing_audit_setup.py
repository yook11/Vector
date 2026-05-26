"""extend stage and category CHECK constraints for briefing audit.

週次 briefing (``app/insights/briefing/``) を ``pipeline_events`` で観測可能にする
ため、CHECK 制約を 2 軸で拡張する:

- stage ``briefing`` を追加 (10 値目)。週次 LLM ブリーフィング生成 stage。
- category ``non_retryable`` を追加 (8 値目)。briefing で intrinsic に retry で
  直らない例外 (``BriefingConfigurationError`` / pydantic ``ValidationError`` /
  DB CONSTRAINT 系) に付ける汎用 non-retry。既存の
  ``non_retryable_drop_article`` / ``non_retryable_keep_article`` /
  ``non_retryable_keep_curation`` は entity 固有後処理を伴う specialization で
  briefing には該当しないため、素の ``non_retryable`` を新設する。

z4_backfill_keep_curate_rename と同型で 2 SSoT (旧/新) tuple を持ち drop →
再作成。過去 row UPDATE は不要 (両値とも新規追加で既存値は touch しない)。

deploy 段取り: 新 CHECK 適用後に旧 worker (Stage.BRIEFING / Layer1Category.
NON_RETRYABLE を知らない) が稼働しても両値とも INSERT してこないので CHECK 違反
は起こらない。新 worker (BriefingAuditRepository を呼ぶ) deploy 後に初めて新値が
書き込まれる始める。

詳細: ``specs/pipeline-events-briefing-audit.md``

Revision ID: z6_briefing_audit_setup
Revises: z5_curation_outcome_rename
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z6_briefing_audit_setup"
down_revision: str | None = "z5_curation_outcome_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Stage CHECK 値: 旧 (9 値) と 新 (briefing 含む 10 値) の 2 SSoT。
# ORM 側 (app/models/pipeline_event.py の __table_args__) と完全一致させる。
_STAGE_VALUES_OLD: tuple[str, ...] = (
    "dispatch",
    "acquisition",
    "completion",
    "curation",
    "assessment",
    "embedding",
    "backfill_curate",
    "backfill_assess",
    "backfill_embed",
)
_STAGE_VALUES_NEW: tuple[str, ...] = (
    "dispatch",
    "acquisition",
    "completion",
    "curation",
    "assessment",
    "embedding",
    "backfill_curate",
    "backfill_assess",
    "backfill_embed",
    "briefing",
)

# category CHECK 値: 旧 (7 値) と 新 (non_retryable 含む 8 値) の 2 SSoT。
# ORM 側と完全一致させる。
_CATEGORY_VALUES_OLD: tuple[str, ...] = (
    "success",
    "idempotent_skip",
    "retryable",
    "non_retryable_drop_article",
    "non_retryable_keep_article",
    "non_retryable_keep_curation",
    "unknown",
)
_CATEGORY_VALUES_NEW: tuple[str, ...] = (
    "success",
    "idempotent_skip",
    "retryable",
    "non_retryable_drop_article",
    "non_retryable_keep_article",
    "non_retryable_keep_curation",
    "non_retryable",
    "unknown",
)


def _stage_check_sql(values: tuple[str, ...]) -> str:
    return "stage IN (" + ",".join(f"'{v}'" for v in values) + ")"


def _category_check_sql(values: tuple[str, ...]) -> str:
    # ORM CHECK は ``category IS NULL OR category IN (...)``。NULL category 行
    # (collection 系 dispatch/acquisition/completion + briefing REJECTED は全て NULL)
    # を許容するため ``IS NULL OR`` を必ず前置する (stage 用 helper との差はここ)。
    inner = ",".join(f"'{v}'" for v in values)
    return f"category IS NULL OR category IN ({inner})"


def upgrade() -> None:
    # 0. lock_timeout: deploy window 内でも他 tx が長く lock を握る事故を予防 (5s)。
    op.execute("SET lock_timeout = '5s';")

    # 1. stage CHECK を drop → 新値 (briefing 含む 10 値) で再作成。
    #    過去 row UPDATE は不要 (briefing は新規 stage で既存行に存在しない)。
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_NEW),
    )

    # 2. category CHECK を drop → 新値 (non_retryable 含む 8 値、IS NULL OR 付き)
    #    で再作成。過去 row UPDATE は不要 (non_retryable は新規 category で既存行
    #    に存在しない)。
    op.drop_constraint("ck_pipeline_events_category", "pipeline_events", type_="check")
    op.create_check_constraint(
        "ck_pipeline_events_category",
        "pipeline_events",
        _category_check_sql(_CATEGORY_VALUES_NEW),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # category を旧値に巻き戻し (downgrade 前提として briefing audit 行は事前に
    # 削除されている想定。実運用では new 値書込後の downgrade は禁止)。
    op.drop_constraint("ck_pipeline_events_category", "pipeline_events", type_="check")
    op.create_check_constraint(
        "ck_pipeline_events_category",
        "pipeline_events",
        _category_check_sql(_CATEGORY_VALUES_OLD),
    )

    # stage を旧値に巻き戻し。
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_OLD),
    )
