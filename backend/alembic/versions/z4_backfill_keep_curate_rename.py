"""rename backfill_extract stage and non_retryable_keep_extraction category to curation.

backfill 系に残った "extraction" 語彙を curation に揃える最終段:

- stage ``backfill_extract`` → ``backfill_curate`` (verb family、curation の backfill)
- category ``non_retryable_keep_extraction`` → ``non_retryable_keep_curation``
  (assessment / embedding が回復不能でも Stage3 の curation 結果を保持する用途)

各々について CHECK の旧値を drop → 既存 row を一括 UPDATE → 新値で CHECK 再作成。
payload->>'kind' は touch しない (backfill は payload を持たず、category は payload
field ではない)。

deploy 段取りは stop-the-world (全 process 停止 → queue drain → migrate →
新 image deploy → resume)。``non_retryable_keep_extraction`` は稼働中の
``AssessmentAuditRepository`` / ``EmbeddingAuditRepository`` の ``_category_of`` が
書き込む live 値であり、category 値変更は本 PR の writer 変更
(``Layer1Category.NON_RETRYABLE_KEEP_CURATION`` への切替) と同時に行う。rolling
deploy で「新 CHECK 適用後に旧 worker が ``'non_retryable_keep_extraction'`` を
INSERT」すると CHECK 違反 (IntegrityError) になるため、旧/新 worker の混在を避ける
必要がある (z1_curation_completion_rename と同型の理由)。stage ``backfill_extract``
は writer 不在 (backfill task は ``Stage.CURATION`` を書く ``curate_content`` に
再投入するだけ) だが、同 migration で一括処理する。

Revision ID: z4_backfill_keep_curate_rename
Revises: z1_curation_completion_rename
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z4_backfill_keep_curate_rename"
down_revision: str | None = "z1_curation_completion_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Stage CHECK 値: 旧 (backfill_extract 含む) と 新 (backfill_curate 含む) の 2 SSoT。
# ORM 側 (app/models/pipeline_event.py の __table_args__) と完全一致させる。
_STAGE_VALUES_OLD: tuple[str, ...] = (
    "dispatch",
    "acquisition",
    "completion",
    "curation",
    "assessment",
    "embedding",
    "backfill_extract",
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
)

# category CHECK 値: 旧 (non_retryable_keep_extraction 含む) と 新
# (non_retryable_keep_curation 含む) の 2 SSoT。ORM 側と完全一致させる。
_CATEGORY_VALUES_OLD: tuple[str, ...] = (
    "success",
    "idempotent_skip",
    "retryable",
    "non_retryable_drop_article",
    "non_retryable_keep_article",
    "non_retryable_keep_extraction",
    "unknown",
)
_CATEGORY_VALUES_NEW: tuple[str, ...] = (
    "success",
    "idempotent_skip",
    "retryable",
    "non_retryable_drop_article",
    "non_retryable_keep_article",
    "non_retryable_keep_curation",
    "unknown",
)


def _stage_check_sql(values: tuple[str, ...]) -> str:
    return "stage IN (" + ",".join(f"'{v}'" for v in values) + ")"


def _category_check_sql(values: tuple[str, ...]) -> str:
    # ORM CHECK は ``category IS NULL OR category IN (...)``。NULL category 行
    # (collection 系 dispatch/acquisition/completion は全て NULL) を許容するため
    # ``IS NULL OR`` を必ず前置する (stage 用 helper との差はここ)。
    inner = ",".join(f"'{v}'" for v in values)
    return f"category IS NULL OR category IN ({inner})"


def upgrade() -> None:
    # 0. lock_timeout: deploy window 内でも他 tx が長く lock を握る事故を予防 (5s)。
    op.execute("SET lock_timeout = '5s';")

    # 1. stage CHECK を drop → 既存 row UPDATE → 新値で再作成。
    #    (backfill_extract は writer 不在で 0 件想定。過去 row 救済のため冪等 UPDATE)
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")
    op.execute(
        "UPDATE pipeline_events "
        "SET stage = 'backfill_curate' WHERE stage = 'backfill_extract'"
    )
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_NEW),
    )

    # 2. category CHECK を drop → 既存 row UPDATE → 新値 (IS NULL OR 付き) で再作成。
    op.drop_constraint("ck_pipeline_events_category", "pipeline_events", type_="check")
    op.execute(
        "UPDATE pipeline_events "
        "SET category = 'non_retryable_keep_curation' "
        "WHERE category = 'non_retryable_keep_extraction'"
    )
    op.create_check_constraint(
        "ck_pipeline_events_category",
        "pipeline_events",
        _category_check_sql(_CATEGORY_VALUES_NEW),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # category を旧値に巻き戻し (CHECK drop → UPDATE → 旧 CHECK 再作成)。
    op.drop_constraint("ck_pipeline_events_category", "pipeline_events", type_="check")
    op.execute(
        "UPDATE pipeline_events "
        "SET category = 'non_retryable_keep_extraction' "
        "WHERE category = 'non_retryable_keep_curation'"
    )
    op.create_check_constraint(
        "ck_pipeline_events_category",
        "pipeline_events",
        _category_check_sql(_CATEGORY_VALUES_OLD),
    )

    # stage を旧値に巻き戻し。
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")
    op.execute(
        "UPDATE pipeline_events "
        "SET stage = 'backfill_extract' WHERE stage = 'backfill_curate'"
    )
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_OLD),
    )
