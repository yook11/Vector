"""会話メッセージと引用 source (ORM)。

message は user 質問 / assistant 回答の 1 行。source は assistant 回答が接地した
引用 (internal 記事 or external URL) で、message の子として単独の意味を持たないため
同一 module に置く。表示契約 (``ResearchResponse``) の完全再現に要る値の非空・型を
DB 制約で焼く。
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["AgentMessage", "AgentMessageSource"]


class AgentMessage(Base):
    """スレッド内の 1 メッセージ (user 質問 or assistant 回答)。"""

    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_agent_messages_thread_seq"),
        # pk の superkey。runs の composite FK 参照先 (設計判断 11)。
        UniqueConstraint("thread_id", "id", name="uq_agent_messages_thread_message"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_agent_messages_role"),
        CheckConstraint("seq >= 1", name="ck_agent_messages_seq_positive"),
        CheckConstraint("content <> ''", name="ck_agent_messages_content_not_empty"),
        # user message は missing_aspects を持たない (設計判断 6)。
        CheckConstraint(
            "role = 'assistant' OR missing_aspects = '[]'::jsonb",
            name="ck_agent_messages_missing_aspects_role",
        ),
        # missing_aspects を JSONB array に限定 (追加制約 A)。要素が非空 str である
        # ことは書き込みファクトリ (slice 2) が保証する。
        CheckConstraint(
            "jsonb_typeof(missing_aspects) = 'array'",
            name="ck_agent_messages_missing_aspects_array",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    thread_id: Mapped[uuid_mod.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "agent_threads.id",
            ondelete="CASCADE",
            name="fk_agent_messages_thread_id",
        ),
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text())
    missing_aspects: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentMessageSource(Base):
    """assistant 回答が接地した引用 source (internal 記事 or external URL)。"""

    __tablename__ = "agent_message_sources"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "source_ref",
            name="uq_agent_message_sources_message_source_ref",
        ),
        UniqueConstraint(
            "message_id",
            "ordinal",
            name="uq_agent_message_sources_message_ordinal",
        ),
        CheckConstraint(
            "kind IN ('internal_article', 'external_url')",
            name="ck_agent_message_sources_kind",
        ),
        CheckConstraint(
            "ordinal >= 1", name="ck_agent_message_sources_ordinal_positive"
        ),
        CheckConstraint(
            "source_ref <> ''", name="ck_agent_message_sources_source_ref_not_empty"
        ),
        CheckConstraint("title <> ''", name="ck_agent_message_sources_title_not_empty"),
        # external_url の恒常条件 (設計判断 8)。url は SafeUrl を正とし DB では
        # scheme(大小無視)/2048 字の粗い backstop。evidence_claim(引用) 必須、
        # analyzed_article_id は持たない。完全な SafeUrl 検証は slice 2 の SafeUrl 型。
        CheckConstraint(
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
        CheckConstraint(
            "kind <> 'internal_article' OR ("
            "url IS NULL AND source_name IS NULL AND evidence_claim IS NULL"
            ")",
            name="ck_agent_message_sources_internal_article",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[uuid_mod.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "agent_messages.id",
            ondelete="CASCADE",
            name="fk_agent_message_sources_message_id",
        ),
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(Text())
    # internal 記事削除後も表示は snapshot で成立させるため SET NULL (設計判断 8)。
    analyzed_article_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "analyzed_articles.id",
            ondelete="SET NULL",
            name="fk_agent_message_sources_analyzed_article_id",
        ),
        nullable=True,
    )
    url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    title: Mapped[str] = mapped_column(Text())
    source_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # external の引用文。internal は記事から引くため NULL (設計判断 8)。
    evidence_claim: Mapped[str | None] = mapped_column(Text(), nullable=True)
