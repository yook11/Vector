"""rename curation outcome_code extracted* to curated_signal/curated_noise.

curation stage (Stage 3) の成功 outcome_code を stage 語彙に整合させる:

- ``extracted`` → ``curated_signal``
- ``extracted_as_noise`` → ``curated_noise``

``extracted`` 系は stage 名が ``extraction`` → ``curation`` へ rename された後
(z1_curation_completion_rename) も wire format として据え置かれた旧 stage 名由来の
化石。``stage='curation'`` の行に ``outcome_code='extracted'`` が残り、兄弟 stage
(assessment の ``assessed_in_scope`` / ``assessed_out_of_scope`` =「動詞過去分詞 +
判定」形式) と乖離していた。本 migration で語彙を統一する。

``outcome_code`` / ``code`` 列は値列挙 CHECK を持たない (String(60) のみ) ため、
z1 と異なり CHECK の drop/recreate は不要。両列に同値が入る (成功 method が
``code=code`` で同じ値を渡す) ため両方を UPDATE する。WHERE は
``stage='curation'`` で絞り他 stage を誤更新しない。no-op 再実行可能。

deploy 段取りは writer 切替 (Stage.CURATION の outcome_code 定数変更) と同時。
rolling deploy で新旧 worker が混在しても、outcome_code に CHECK が無いため
IntegrityError は起きない (混在中は新旧両値が並ぶだけ)。

Revision ID: z5_curation_outcome_rename
Revises: z4_backfill_keep_curate_rename
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z5_curation_outcome_rename"
down_revision: str | None = "z4_backfill_keep_curate_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # extracted → curated_signal (outcome_code + code、no-op 再実行可能)。
    op.execute(
        "UPDATE pipeline_events "
        "SET outcome_code = 'curated_signal', code = 'curated_signal' "
        "WHERE stage = 'curation' AND outcome_code = 'extracted'"
    )
    # extracted_as_noise → curated_noise (outcome_code + code)。
    op.execute(
        "UPDATE pipeline_events "
        "SET outcome_code = 'curated_noise', code = 'curated_noise' "
        "WHERE stage = 'curation' AND outcome_code = 'extracted_as_noise'"
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    op.execute(
        "UPDATE pipeline_events "
        "SET outcome_code = 'extracted', code = 'extracted' "
        "WHERE stage = 'curation' AND outcome_code = 'curated_signal'"
    )
    op.execute(
        "UPDATE pipeline_events "
        "SET outcome_code = 'extracted_as_noise', code = 'extracted_as_noise' "
        "WHERE stage = 'curation' AND outcome_code = 'curated_noise'"
    )
