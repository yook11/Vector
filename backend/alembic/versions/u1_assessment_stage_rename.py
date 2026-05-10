"""rename pipeline_events.stage value classification to assessment, add layer1 category.

PR4: Stage 4 (Assessment) refactor の DB 側準備。

- pipeline_events.stage CHECK から 'classification' を drop、'assessment' を add
- 既存 row の stage='classification' を 'assessment' に一括 UPDATE
- 既存 row の payload->>'kind'='classification' を 'assessment' に jsonb_set UPDATE
- pipeline_events.category CHECK に 'non_retryable_keep_extraction' を add

deploy 段取りは t1_assessment_table_rename (PR3.5-d.1) と同じ stop-the-world
(全 process 停止 → migrate → 新 image deploy → resume)。

数万行 UPDATE による lock 影響を避けるため deploy window 推奨。

Revision ID: u1_assessment_stage_rename
Revises: t1_assessment_table_rename
Create Date: 2026-05-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "u1_assessment_stage_rename"
down_revision: str | None = "t1_assessment_table_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Stage CHECK 値: 旧 (classification 含む) と 新 (assessment 含む) の 2 SSoT。
# ORM 側 (app/models/pipeline_event.py の __table_args__) と完全一致させる。
_STAGE_VALUES_OLD: tuple[str, ...] = (
    "dispatch",
    "source_fetch",
    "content_fetch",
    "extraction",
    "classification",
    "embedding",
    "backfill_extract",
    "backfill_classify",
    "backfill_embed",
)
_STAGE_VALUES_NEW: tuple[str, ...] = (
    "dispatch",
    "source_fetch",
    "content_fetch",
    "extraction",
    "assessment",
    "embedding",
    "backfill_extract",
    "backfill_classify",
    "backfill_embed",
)

# Layer1Category CHECK 値: 旧 6 と 新 7 (non_retryable_keep_extraction 追加)。
_CATEGORY_VALUES_OLD: tuple[str, ...] = (
    "success",
    "idempotent_skip",
    "retryable",
    "non_retryable_drop_article",
    "non_retryable_keep_article",
    "unknown",
)
_CATEGORY_VALUES_NEW: tuple[str, ...] = (
    "success",
    "idempotent_skip",
    "retryable",
    "non_retryable_drop_article",
    "non_retryable_keep_article",
    "non_retryable_keep_extraction",
    "unknown",
)


def _stage_check_sql(values: tuple[str, ...]) -> str:
    return "stage IN (" + ",".join(f"'{v}'" for v in values) + ")"


def _category_check_sql(values: tuple[str, ...]) -> str:
    inner = ",".join(f"'{v}'" for v in values)
    return f"category IS NULL OR category IN ({inner})"


def upgrade() -> None:
    # 0. lock_timeout: deploy window 内でも他 tx が長く lock を握る事故を予防。
    #    t1_assessment_table_rename と同じ pattern (5s)。
    op.execute("SET lock_timeout = '5s';")

    # 1. stage CHECK を drop してから既存 row を UPDATE (新 CHECK は最後に張り直す)。
    #    順序: CHECK drop → UPDATE → CHECK 再作成 (中間で row が CHECK 違反にならない)
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")

    # 2. stage 既存 row UPDATE (UPDATE 自体は no-op 再実行可能: 既に 'assessment' の
    #    row は WHERE 句で対象外になるだけ、再実行で row が壊れない)。
    op.execute(
        "UPDATE pipeline_events SET stage = 'assessment' WHERE stage = 'classification'"
    )

    # 3. payload.kind 既存 row UPDATE (jsonb_set、UPDATE は no-op 再実行可能)。
    op.execute(
        "UPDATE pipeline_events "
        "SET payload = jsonb_set(payload, '{kind}', '\"assessment\"'::jsonb) "
        "WHERE payload->>'kind' = 'classification'"
    )

    # 4. stage CHECK を新値で再作成。
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_NEW),
    )

    # 5. category CHECK を新値 (non_retryable_keep_extraction 追加) で張り直し。
    op.drop_constraint("ck_pipeline_events_category", "pipeline_events", type_="check")
    op.create_check_constraint(
        "ck_pipeline_events_category",
        "pipeline_events",
        _category_check_sql(_CATEGORY_VALUES_NEW),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # category 新値を旧値に戻す。もし non_retryable_keep_extraction の row があれば
    # CHECK 違反で fail = 想定通り (PR5/PR6 を deploy 済の状態を巻き戻すなら手動で
    # row を別 category に振り直してから downgrade する必要がある)。
    op.drop_constraint("ck_pipeline_events_category", "pipeline_events", type_="check")
    op.create_check_constraint(
        "ck_pipeline_events_category",
        "pipeline_events",
        _category_check_sql(_CATEGORY_VALUES_OLD),
    )

    # stage CHECK drop → UPDATE で巻き戻し → 旧 CHECK 再作成。
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")
    op.execute(
        "UPDATE pipeline_events SET stage = 'classification' WHERE stage = 'assessment'"
    )
    op.execute(
        "UPDATE pipeline_events "
        "SET payload = jsonb_set(payload, '{kind}', '\"classification\"'::jsonb) "
        "WHERE payload->>'kind' = 'assessment'"
    )
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_OLD),
    )
