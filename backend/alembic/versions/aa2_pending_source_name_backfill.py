"""backfill pending_html_articles.source_name from news_sources.name (step 3b).

PendingHtmlArticle の source identity を表層列に昇格する 3 段 migration の
**Step 3b**。3a (aa1) で nullable 列を追加した後、本 step で
``source_id`` JOIN により ``news_sources.name`` を全行にコピーする。

legacy 行 (``staged_attributes->>'sourceName'`` 欠落) と新形行
(``schemaVersion=1`` で JSONB に ``sourceName`` を持つ) を区別せず、すべて
``news_sources.id → news_sources.name`` の **正規ルート 1 本** で埋める。
JSONB の ``sourceName`` (新形行) は同値の denormalize copy のはずなので
読まない (もし不整合があれば news_sources を SSoT として上書き)。

3c (aa3) で NOT NULL + composite FK を張る前提として、backfill 後に
``source_name IS NULL`` の行が残らないことを検証する。

Revision ID: aa2_pending_source_name_backfill
Revises: aa1_pending_source_name_add
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa2_pending_source_name_backfill"
down_revision: str | None = "aa1_pending_source_name_add"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")

    # backfill: source_id JOIN で news_sources.name を全行にコピー。
    # 既に source_name が埋まっている行 (アプリ側で既に書き始めている等) は
    # 上書きしない (= news_sources と既存 source_name が一致している前提で
    # WHERE source_name IS NULL に絞る)。production で一致しない行があれば
    # 検証ブロックで RAISE EXCEPTION する。
    op.execute(
        sa.text(
            "UPDATE pending_html_articles p "
            "SET source_name = ns.name "
            "FROM news_sources ns "
            "WHERE p.source_id = ns.id "
            "  AND p.source_name IS NULL"
        )
    )

    # 検証 (a): backfill 後に NULL が残っていないこと (= 全 pending 行で
    # source_id が news_sources を指している前提が成立している証拠。
    # 単独 FK ``source_id → news_sources.id`` で保証されている範囲だが、
    # backfill 失敗の早期検出に保険として残す)。
    op.execute(
        sa.text(
            "DO $$ "
            "DECLARE unfilled int; "
            "BEGIN "
            "  SELECT COUNT(*) INTO unfilled FROM pending_html_articles "
            "  WHERE source_name IS NULL; "
            "  IF unfilled > 0 THEN "
            "    RAISE EXCEPTION '% pending_html_articles still have NULL "
            "source_name after backfill', unfilled; "
            "  END IF; "
            "END $$;"
        )
    )

    # 検証 (b): backfill 後の source_name が news_sources.name と一致
    # (= denormalize copy の出処が news_sources であることの証拠。3c で
    # composite FK を張るので一致しない行があれば必ず弾かれるが、3c より
    # 早い段階で発見する保険)。
    op.execute(
        sa.text(
            "DO $$ "
            "DECLARE mismatch int; "
            "BEGIN "
            "  SELECT COUNT(*) INTO mismatch FROM pending_html_articles p "
            "  JOIN news_sources ns ON ns.id = p.source_id "
            "  WHERE p.source_name IS DISTINCT FROM ns.name; "
            "  IF mismatch > 0 THEN "
            "    RAISE EXCEPTION '% pending_html_articles have source_name "
            "diverging from news_sources.name after backfill', mismatch; "
            "  END IF; "
            "END $$;"
        )
    )


def downgrade() -> None:
    # backfill は forward-only (列値を NULL に戻す意味がない、3a/3c で巻き戻す)。
    raise NotImplementedError(
        "forward-only: source_name backfill values are part of the structural "
        "invariant; downgrade by reverting aa3 (constraints) then aa1 (column)"
    )
