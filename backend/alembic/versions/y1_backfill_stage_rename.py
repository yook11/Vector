"""rename pipeline_events.stage value backfill_classify to backfill_assess.

PR-3b: PR-1〜PR-3a までで完了した Classifier → Assessor rename の最終段。
backfill stage 値だけが旧名のまま残っていたのを統一する。

- pipeline_events.stage CHECK から 'backfill_classify' を drop、'backfill_assess' を add
- 既存 row の stage='backfill_classify' を 'backfill_assess' に一括 UPDATE
  (Vector はまだ本番未デプロイなので実質 no-op の想定だが冪等に書く)

deploy 段取りは u1_assessment_stage_rename と同じ stop-the-world pattern。

Revision ID: y1_backfill_stage_rename
Revises: x1_oos_snapshot_strict
Create Date: 2026-05-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "y1_backfill_stage_rename"
down_revision: str | None = "x1_oos_snapshot_strict"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Stage CHECK 値: 旧 (backfill_classify 含む) と 新 (backfill_assess 含む) の 2 SSoT。
# ORM 側 (app/models/pipeline_event.py の __table_args__) と完全一致させる。
_STAGE_VALUES_OLD: tuple[str, ...] = (
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
_STAGE_VALUES_NEW: tuple[str, ...] = (
    "dispatch",
    "source_fetch",
    "content_fetch",
    "extraction",
    "assessment",
    "embedding",
    "backfill_extract",
    "backfill_assess",
    "backfill_embed",
)


def _stage_check_sql(values: tuple[str, ...]) -> str:
    return "stage IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    # 0. lock_timeout: deploy window 内でも他 tx が長く lock を握る事故を予防。
    #    u1_assessment_stage_rename と同じ pattern (5s)。
    op.execute("SET lock_timeout = '5s';")

    # 1. stage CHECK を drop してから既存 row を UPDATE (新 CHECK は最後に張り直す)。
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")

    # 2. stage 既存 row UPDATE (再実行で row が壊れない冪等な UPDATE)。
    op.execute(
        "UPDATE pipeline_events SET stage = 'backfill_assess' "
        "WHERE stage = 'backfill_classify'"
    )

    # 3. stage CHECK を新値で再作成。
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_NEW),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")
    op.execute(
        "UPDATE pipeline_events SET stage = 'backfill_classify' "
        "WHERE stage = 'backfill_assess'"
    )
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_OLD),
    )
