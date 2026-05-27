"""Drop Stage 3 entities artifacts + obsolete trend snapshots.

PR 2 of event-extraction migration. mention 軸への Insights 集計切替と同 PR。

upgrade:
  1. ``weekly_trend_snapshots`` を空にする (旧 ``EntityType`` 値域で永続化された
     ``WeeklyTrendsBundle`` を ``MentionType`` 化後にロードすると Pydantic
     ValidationError になるため、PR 2 マージ前に廃棄して再蓄積する設計)。
     snapshot は週次再生成可能なので blast radius が小さい。
  2. ``article_extraction_entities`` テーブル drop。Stage 3 の (surface, raw_type)
     台帳は Stage 4 ``in_scope_assessments.events[].mentions[]`` JSONB に役割を
     譲り、SSoT が assessment 側へ移ったため業務側からは不要。
  3. ``extraction_noises.entities`` JSONB カラム drop (CHECK 制約も削除)。
     Noise 行も entity 構造を保持しなくなるため。

downgrade:
  schema 再作成のみ。過去データは復元されない (article_extraction_entities /
  weekly_trend_snapshots の rows は失われる)。production rollback は実質
  不可で、dev verification で十分検証してから merge する前提。

Revision ID: a2_drop_extraction_entities
Revises: a1_assessments_add_events
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2_drop_extraction_entities"
down_revision: str | None = "a1_assessments_add_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # deploy window 内で他 tx が長く lock を握る事故を予防 (recent pattern と整合)。
    op.execute("SET lock_timeout = '5s';")

    # 1. 旧 EntityType 値域の snapshot を全削除 (新 MentionType 軸で再生成される)。
    op.execute("DELETE FROM weekly_trends_snapshots")

    # 2. article_extraction_entities テーブル drop (CASCADE で index も消える)。
    op.drop_index(
        "ix_article_extraction_entities_extraction_id",
        table_name="article_extraction_entities",
    )
    op.drop_table("article_extraction_entities")

    # 3. extraction_noises.entities カラム drop (CHECK 制約も付随削除)。
    op.drop_constraint(
        "ck_extraction_noises_entities_is_array",
        "extraction_noises",
        type_="check",
    )
    op.drop_column("extraction_noises", "entities")


def downgrade() -> None:
    # 1. extraction_noises.entities 復元 (l8_aee_create 後の運用形と同型)。
    op.add_column(
        "extraction_noises",
        sa.Column(
            "entities",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_extraction_noises_entities_is_array",
        "extraction_noises",
        "jsonb_typeof(entities) = 'array'",
    )

    # 2. article_extraction_entities テーブル復元 (l8_aee_create.py と bit-identical)。
    #    schema 再作成のみで過去データは復元されない。
    op.create_table(
        "article_extraction_entities",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "extraction_id",
            sa.Integer(),
            sa.ForeignKey("article_extractions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("surface", sa.String(length=200), nullable=False),
        sa.Column("raw_type", sa.String(length=30), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("surface != ''", name="ck_aee_surface_not_empty"),
        sa.CheckConstraint("raw_type != ''", name="ck_aee_raw_type_not_empty"),
    )
    op.create_index(
        "ix_article_extraction_entities_extraction_id",
        "article_extraction_entities",
        ["extraction_id"],
    )

    # 3. weekly_trend_snapshots の rows は復元不可 (削除済 snapshot は再構築できない、
    #    次回 weekly batch まで empty)。
