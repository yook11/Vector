"""AI Q&A エージェントの会話スレッド (ORM)。

user が所有する会話の親エンティティ。物理削除は auth.user / thread の
``ondelete=CASCADE`` に任せ、ORM relationship は消費側 slice で追加する。
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["AgentThread"]


class AgentThread(Base):
    """user が所有する会話スレッド。"""

    __tablename__ = "agent_threads"
    __table_args__ = (
        CheckConstraint("title <> ''", name="ck_agent_threads_title_not_empty"),
        # 「user の thread を最終活動順」一覧クエリ向けの複合 DESC index。
        Index(
            "ix_agent_threads_user_updated",
            "user_id",
            text("updated_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid_mod.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("auth.user.id", ondelete="CASCADE", name="fk_agent_threads_user_id"),
    )
    title: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # 最終活動時刻 (app 管理・onupdate なし)。初期値のみ server_default (設計判断 3)。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
