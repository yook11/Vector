"""add agent conversation history tables

AI Q&A エージェントの会話履歴・実行状態を永続化する 4 テーブル
(agent_threads / agent_messages / agent_message_sources / agent_runs)。
親仕様の Invariants (user 分離 / 1 thread 1 active run / 1 user message 1 run /
表示契約の完全再現 / 物理削除 cascade) を unique / check / composite FK /
partial unique index として DB に焼く。データ層のみ (保存経路・API は後続 slice)。

Revision ID: y1_agent_history
Revises: x8_published_at_not_null
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from alembic import op

revision: str = "y1_agent_history"
down_revision: str | None = "x8_published_at_not_null"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# migration_gate: 新規テーブル 4 つの追加のみ (op.create_table + inline 制約/index、
# 破壊系・op.execute なし)。partial unique index 含め index はすべて create_table 内
# inline sa.Index で作る (新規空テーブルで lock 問題が無いため CONCURRENTLY 不要)。
MIGRATION_KIND = "expand"


def upgrade() -> None:
    op.create_table(
        "agent_threads",
        sa.Column(
            "id",
            PgUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey(
                "auth.user.id",
                ondelete="CASCADE",
                name="fk_agent_threads_user_id",
            ),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("title <> ''", name="ck_agent_threads_title_not_empty"),
        # 「user の thread を最終活動順」一覧クエリ向けの複合 DESC index。
        sa.Index(
            "ix_agent_threads_user_updated",
            "user_id",
            sa.text("updated_at DESC"),
            sa.text("id DESC"),
        ),
    )

    op.create_table(
        "agent_messages",
        sa.Column(
            "id",
            PgUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "thread_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey(
                "agent_threads.id",
                ondelete="CASCADE",
                name="fk_agent_messages_thread_id",
            ),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "missing_aspects",
            JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("thread_id", "seq", name="uq_agent_messages_thread_seq"),
        # pk の superkey。runs の composite FK 参照先 (設計判断 11)。
        sa.UniqueConstraint("thread_id", "id", name="uq_agent_messages_thread_message"),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')", name="ck_agent_messages_role"
        ),
        sa.CheckConstraint("seq >= 1", name="ck_agent_messages_seq_positive"),
        sa.CheckConstraint("content <> ''", name="ck_agent_messages_content_not_empty"),
        # user message は missing_aspects を持たない (設計判断 6)。
        sa.CheckConstraint(
            "role = 'assistant' OR missing_aspects = '[]'::jsonb",
            name="ck_agent_messages_missing_aspects_role",
        ),
        # missing_aspects を JSONB array に限定 (追加制約 A)。要素が非空 str である
        # ことは書き込みファクトリ (slice 2) が保証する。
        sa.CheckConstraint(
            "jsonb_typeof(missing_aspects) = 'array'",
            name="ck_agent_messages_missing_aspects_array",
        ),
    )

    op.create_table(
        "agent_message_sources",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "message_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey(
                "agent_messages.id",
                ondelete="CASCADE",
                name="fk_agent_message_sources_message_id",
            ),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        # internal 記事削除後も表示は snapshot で成立させるため SET NULL (設計判断 8)。
        sa.Column(
            "analyzed_article_id",
            sa.Integer(),
            sa.ForeignKey(
                "analyzed_articles.id",
                ondelete="SET NULL",
                name="fk_agent_message_sources_analyzed_article_id",
            ),
            nullable=True,
        ),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_claim", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "message_id",
            "source_ref",
            name="uq_agent_message_sources_message_source_ref",
        ),
        sa.UniqueConstraint(
            "message_id", "ordinal", name="uq_agent_message_sources_message_ordinal"
        ),
        sa.CheckConstraint(
            "kind IN ('internal_article', 'external_url')",
            name="ck_agent_message_sources_kind",
        ),
        sa.CheckConstraint(
            "ordinal >= 1", name="ck_agent_message_sources_ordinal_positive"
        ),
        sa.CheckConstraint(
            "source_ref <> ''", name="ck_agent_message_sources_source_ref_not_empty"
        ),
        sa.CheckConstraint(
            "title <> ''", name="ck_agent_message_sources_title_not_empty"
        ),
        # external_url の恒常条件 (設計判断 8)。url は SafeUrl を正とし DB では
        # scheme(大小無視)/2048 字の粗い backstop。evidence_claim(引用) 必須、
        # analyzed_article_id は持たない。完全な SafeUrl 検証は slice 2 の SafeUrl 型。
        sa.CheckConstraint(
            "kind <> 'external_url' OR ("
            "url IS NOT NULL AND url ~* '^https?://' AND char_length(url) <= 2048 "
            "AND analyzed_article_id IS NULL "
            "AND evidence_claim IS NOT NULL AND evidence_claim <> ''"
            ")",
            name="ck_agent_message_sources_external_url",
        ),
        # internal_article の恒常条件 (設計判断 8)。記事から引くため url / source_name /
        # evidence_claim は持たない。analyzed_article_id は insert 時 app-layer で
        # 非 NULL 保証し、記事削除後の NULL は SET NULL による正当状態 (設計判断 15)。
        sa.CheckConstraint(
            "kind <> 'internal_article' OR ("
            "url IS NULL AND source_name IS NULL AND evidence_claim IS NULL"
            ")",
            name="ck_agent_message_sources_internal_article",
        ),
    )

    op.create_table(
        "agent_runs",
        sa.Column(
            "id",
            PgUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "thread_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey(
                "agent_threads.id",
                ondelete="CASCADE",
                name="fk_agent_runs_thread_id",
            ),
            nullable=False,
        ),
        sa.Column("user_message_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("assistant_message_id", PgUUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress_stage", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # run と message の同一 thread を composite FK で強制 (設計判断 11)。
        # assistant_message_id NULL の間は MATCH SIMPLE で検査対象外。
        sa.ForeignKeyConstraint(
            ["thread_id", "user_message_id"],
            ["agent_messages.thread_id", "agent_messages.id"],
            ondelete="CASCADE",
            name="fk_agent_runs_thread_user_message",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id", "assistant_message_id"],
            ["agent_messages.thread_id", "agent_messages.id"],
            ondelete="CASCADE",
            name="fk_agent_runs_thread_assistant_message",
        ),
        sa.UniqueConstraint("user_message_id", name="uq_agent_runs_user_message"),
        # 1 completed run = 1 回答 message (追加制約 B、NULL 複数可)。
        sa.UniqueConstraint(
            "assistant_message_id", name="uq_agent_runs_assistant_message"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_agent_runs_status",
        ),
        sa.CheckConstraint(
            "progress_stage IN ('planning', 'retrieving', 'synthesizing')",
            name="ck_agent_runs_progress_stage",
        ),
        # 完了 run は必ず回答を持ち、未完了・失敗は持たない (設計判断 5)。
        sa.CheckConstraint(
            "(status = 'completed') = (assistant_message_id IS NOT NULL)",
            name="ck_agent_runs_completed_answer",
        ),
        # failed ⇔ error_code の双方向 (設計判断 12)。
        sa.CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name="ck_agent_runs_failed_error",
        ),
        # 1 thread に active(queued/running) run は同時 1 本 (設計判断 10)。
        sa.Index(
            "uq_agent_runs_thread_active",
            "thread_id",
            unique=True,
            postgresql_where=sa.text("status IN ('queued', 'running')"),
        ),
        sa.Index("ix_agent_runs_thread", "thread_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_runs")
    op.drop_table("agent_message_sources")
    op.drop_table("agent_messages")
    op.drop_table("agent_threads")
