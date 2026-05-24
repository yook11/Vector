"""rename pipeline_events.stage value source_fetch to acquisition.

stage1 の語彙統一 (collection/article_acquisition への改名) に伴い、観測トークンも
``source_fetch`` から ``acquisition`` に揃える。

- pipeline_events.stage CHECK から 'source_fetch' を drop、'acquisition' を add
- 既存 row の stage='source_fetch' を 'acquisition' に一括 UPDATE
- 既存 row の payload->>'kind'='source_fetch' を 'acquisition' に jsonb_set UPDATE

deploy 段取りは u1_assessment_stage_rename と同じ stop-the-world
(全 process 停止 → queue drain → migrate → 新 image deploy → resume)。
taskiq task も同 PR で ingest_source → acquire_source に改名するため、旧/新 worker
混在を避ける必要がある。

数万行 UPDATE による lock 影響を避けるため deploy window 推奨。

Revision ID: v1_acquisition_stage_rename
Revises: u1_incomplete_articles_rename
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "v1_acquisition_stage_rename"
down_revision: str | None = "u1_incomplete_articles_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Stage CHECK 値: 旧 (source_fetch 含む) と 新 (acquisition 含む) の 2 SSoT。
# ORM 側 (app/models/pipeline_event.py の __table_args__) と完全一致させる。
_STAGE_VALUES_OLD: tuple[str, ...] = (
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
_STAGE_VALUES_NEW: tuple[str, ...] = (
    "dispatch",
    "acquisition",
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
    # 0. lock_timeout: deploy window 内でも他 tx が長く lock を握る事故を予防 (5s)。
    op.execute("SET lock_timeout = '5s';")

    # 1. stage CHECK を drop してから既存 row を UPDATE (新 CHECK は最後に張り直す)。
    #    順序: CHECK drop → UPDATE → CHECK 再作成 (中間で row が CHECK 違反にならない)
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")

    # 2. stage 既存 row UPDATE (no-op 再実行可能: 既に 'acquisition' の row は WHERE
    #    句で対象外、再実行で row が壊れない)。
    op.execute(
        "UPDATE pipeline_events SET stage = 'acquisition' WHERE stage = 'source_fetch'"
    )

    # 3. payload.kind 既存 row UPDATE (jsonb_set、no-op 再実行可能)。
    op.execute(
        "UPDATE pipeline_events "
        "SET payload = jsonb_set(payload, '{kind}', '\"acquisition\"'::jsonb) "
        "WHERE payload->>'kind' = 'source_fetch'"
    )

    # 4. stage CHECK を新値で再作成。
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_NEW),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # stage CHECK drop → UPDATE で巻き戻し → 旧 CHECK 再作成。
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")
    op.execute(
        "UPDATE pipeline_events SET stage = 'source_fetch' WHERE stage = 'acquisition'"
    )
    op.execute(
        "UPDATE pipeline_events "
        "SET payload = jsonb_set(payload, '{kind}', '\"source_fetch\"'::jsonb) "
        "WHERE payload->>'kind' = 'acquisition'"
    )
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_OLD),
    )
