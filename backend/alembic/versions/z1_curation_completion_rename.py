"""rename pipeline_events.stage values extraction/content_fetch to curation/completion.

stage 値を処理モジュール名に揃える最終段:

- ``extraction`` → ``curation`` (analysis/curation の語彙統一)
- ``content_fetch`` → ``completion`` (collection/article_completion の語彙統一)

各々について:
- pipeline_events.stage CHECK の旧値を drop、新値を add
- 既存 row の stage を一括 UPDATE
- 既存 row の payload->>'kind' を jsonb_set で UPDATE

deploy 段取りは stop-the-world (全 process 停止 → queue drain → migrate →
新 image deploy → resume)。``extraction`` は稼働中の ``CurationAuditRepository``
が書き込む live 値であり、stage 値変更は本 PR の writer 変更 (Stage.CURATION への
切替) と同時に行う。rolling deploy で「新 CHECK 適用後に旧 worker が
``'extraction'`` を INSERT」すると CHECK 違反 (IntegrityError) になるため、旧/新
worker の混在を避ける必要がある (v1_acquisition_stage_rename と同型の理由)。

数万行 UPDATE による lock 影響を避けるため deploy window 推奨。

Revision ID: z1_curation_completion_rename
Revises: v1_acquisition_stage_rename
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z1_curation_completion_rename"
down_revision: str | None = "v1_acquisition_stage_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Stage CHECK 値: 旧 (extraction / content_fetch 含む) と 新 (curation /
# completion 含む) の 2 SSoT。ORM 側 (app/models/pipeline_event.py の
# __table_args__) と完全一致させる。
_STAGE_VALUES_OLD: tuple[str, ...] = (
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
_STAGE_VALUES_NEW: tuple[str, ...] = (
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


def _stage_check_sql(values: tuple[str, ...]) -> str:
    return "stage IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    # 0. lock_timeout: deploy window 内でも他 tx が長く lock を握る事故を予防 (5s)。
    op.execute("SET lock_timeout = '5s';")

    # 1. stage CHECK を drop してから既存 row を UPDATE (新 CHECK は最後に張り直す)。
    #    順序: CHECK drop → UPDATE → CHECK 再作成 (中間で row が CHECK 違反にならない)
    op.drop_constraint("ck_pipeline_events_stage", "pipeline_events", type_="check")

    # 2. extraction → curation (stage 列 + payload.kind、no-op 再実行可能)。
    op.execute(
        "UPDATE pipeline_events SET stage = 'curation' WHERE stage = 'extraction'"
    )
    op.execute(
        "UPDATE pipeline_events "
        "SET payload = jsonb_set(payload, '{kind}', '\"curation\"'::jsonb) "
        "WHERE payload->>'kind' = 'extraction'"
    )

    # 3. content_fetch → completion (stage 列 + payload.kind、no-op 再実行可能)。
    op.execute(
        "UPDATE pipeline_events SET stage = 'completion' WHERE stage = 'content_fetch'"
    )
    op.execute(
        "UPDATE pipeline_events "
        "SET payload = jsonb_set(payload, '{kind}', '\"completion\"'::jsonb) "
        "WHERE payload->>'kind' = 'content_fetch'"
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
        "UPDATE pipeline_events SET stage = 'extraction' WHERE stage = 'curation'"
    )
    op.execute(
        "UPDATE pipeline_events "
        "SET payload = jsonb_set(payload, '{kind}', '\"extraction\"'::jsonb) "
        "WHERE payload->>'kind' = 'curation'"
    )
    op.execute(
        "UPDATE pipeline_events SET stage = 'content_fetch' WHERE stage = 'completion'"
    )
    op.execute(
        "UPDATE pipeline_events "
        "SET payload = jsonb_set(payload, '{kind}', '\"content_fetch\"'::jsonb) "
        "WHERE payload->>'kind' = 'completion'"
    )
    op.create_check_constraint(
        "ck_pipeline_events_stage",
        "pipeline_events",
        _stage_check_sql(_STAGE_VALUES_OLD),
    )
