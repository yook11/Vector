"""1 user message に対する 1 回の非同期実行状態 (ORM)。

run は状態機械 (queued → running → completed/failed)。thread / message との
同一 thread 整合を composite FK で焼く (設計判断 11)。ORM relationship は
消費側 slice で追加する。
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["AgentRun"]


class AgentRun(Base):
    """1 user message に対する 1 回の実行状態。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        # run と message の同一 thread を composite FK で強制 (設計判断 11)。
        # assistant_message_id NULL の間は MATCH SIMPLE で検査対象外。
        ForeignKeyConstraint(
            ["thread_id", "user_message_id"],
            ["agent_messages.thread_id", "agent_messages.id"],
            ondelete="CASCADE",
            name="fk_agent_runs_thread_user_message",
        ),
        ForeignKeyConstraint(
            ["thread_id", "assistant_message_id"],
            ["agent_messages.thread_id", "agent_messages.id"],
            ondelete="CASCADE",
            name="fk_agent_runs_thread_assistant_message",
        ),
        UniqueConstraint("user_message_id", name="uq_agent_runs_user_message"),
        # 1 completed run = 1 回答 message (追加制約 B、NULL 複数可)。
        UniqueConstraint(
            "assistant_message_id", name="uq_agent_runs_assistant_message"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint(
            "progress_stage IN ('planning', 'retrieving', 'synthesizing')",
            name="ck_agent_runs_progress_stage",
        ),
        CheckConstraint(
            "attempt_epoch >= 0",
            name="ck_agent_runs_attempt_epoch_nonnegative",
        ),
        # 完了 run は必ず回答を持ち、未完了・失敗は持たない (設計判断 5)。
        CheckConstraint(
            "(status = 'completed') = (assistant_message_id IS NOT NULL)",
            name="ck_agent_runs_completed_answer",
        ),
        # failed ⇔ error_code の双方向 (設計判断 12)。
        CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name="ck_agent_runs_failed_error",
        ),
        # 1 thread に active(queued/running) run は同時 1 本 (設計判断 10)。
        Index(
            "uq_agent_runs_thread_active",
            "thread_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_agent_runs_thread", "thread_id"),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    thread_id: Mapped[uuid_mod.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "agent_threads.id", ondelete="CASCADE", name="fk_agent_runs_thread_id"
        ),
    )
    user_message_id: Mapped[uuid_mod.UUID] = mapped_column(PgUUID(as_uuid=True))
    assistant_message_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32))
    progress_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_epoch: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default=text("0")
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
