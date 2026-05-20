"""``pending_html_articles`` テーブル — HTML 取得待ちの作業領域 (Pattern H 専用)。

PR2.5-A 新設。Stage 1 で entry が ``Failed`` (RSS で本文不足等) と判定された場合、
この行が作られ、Stage 2 で HTML 取得を行うキューとなる。
lease 方式 (status=open/running/closed + ready_at + leased_until +
attempt_count) で多 worker 安全な claim と sweeper による lease 救出を行う。

設計詳細は ``specs/pipeline-events-stage2-design.md`` の §データフロー §スキーマ案。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import SafeUrlType, SourceNameType
from app.shared.value_objects.safe_url import SafeUrl
from app.shared.value_objects.source_name import SourceName


class PendingHtmlArticle(Base):
    """HTML 取得待ちのキュー行 (lease 方式)。

    state model:
    - ``open``  — 未 claim、``ready_at`` の到来を待つ picking 候補。
                  ``ready_at`` が未来なら backoff 中、過去なら次 cron で picking 可能。
                  ``leased_until`` は NULL。
    - ``running`` — worker が claim 済 (lease 期限内)、``leased_until`` が値を持つ。
    - ``closed`` — 永続失敗 / retry 予算切れ (再試行しない)、``leased_until`` は NULL。

    state 整合性は CHECK 制約で構造的に強制する:
    - status × leased_until の組合せ不整合 (open なのに lease 残り 等) を遮断
    - open / running は ``ready_at`` 必須 (NULL だと worker が永遠に拾わない)
    - closed は ``ready_at`` を NULL でも値持ちでも許容 (再試行しないので無視される)

    lease 期限切れの ``running`` 行は別 cron の sweeper が ``open`` に戻す。

    ``url`` の UNIQUE が articles と pending の cross-table dedup の物理保証
    (caller は canonicalize 済み URL を渡すこと)。
    """

    __tablename__ = "pending_html_articles"
    __table_args__ = (
        UniqueConstraint("url", name="uq_pending_html_articles_url"),
        # composite FK (source_id, source_name) → news_sources(id, name)。
        # ``news_sources`` 側の ``(id, name)`` UNIQUE を target にし、
        # ``source_id`` 単独更新 (source_name は旧値のまま) のような drift
        # を DB で構造的に遮断する (spec ``Pending source identity refactor.md``
        # #2)。単独 FK ``source_id → news_sources.id`` も維持し、
        # 読み手に「source_id は news_sources の id を指す」単独不変条件を
        # 明示する。
        ForeignKeyConstraint(
            ["source_id", "source_name"],
            ["news_sources.id", "news_sources.name"],
            name="fk_pending_html_articles_source_id_name",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "url ~ '^https?://.+'",
            name="ck_pending_html_articles_url_scheme",
        ),
        CheckConstraint(
            "status IN ('open','running','closed')",
            name="ck_pending_html_articles_status",
        ),
        # state ↔ leased_until の整合性
        CheckConstraint(
            "(status = 'open'    AND leased_until IS NULL) OR "
            "(status = 'running' AND leased_until IS NOT NULL) OR "
            "(status = 'closed'  AND leased_until IS NULL)",
            name="ck_pending_html_articles_state_consistency",
        ),
        # open / running は ready_at 必須 (NULL だと picking から漏れる事故を防ぐ)
        CheckConstraint(
            "(status IN ('open','running') AND ready_at IS NOT NULL) OR "
            "(status = 'closed')",
            name="ck_pending_html_articles_ready_required",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_pending_html_articles_attempt_nonneg",
        ),
        # picking 用 (open のみ、ready_at で order)
        Index(
            "ix_pending_html_articles_ready",
            "ready_at",
            postgresql_where=text("status = 'open'"),
        ),
        # sweeper 用 (running のみ、leased_until で order)
        Index(
            "ix_pending_html_articles_expired_lease",
            "leased_until",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    url: Mapped[SafeUrl] = mapped_column(SafeUrlType, nullable=False)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("news_sources.id", ondelete="RESTRICT"),
    )
    # domain identity (news_sources.name の denormalized copy)。
    # composite FK ``(source_id, source_name) → news_sources(id, name)`` で
    # 2 表現の整合を DB で構造保証する (spec ``Pending source identity
    # refactor.md``)。Migration は 3 段 (3a nullable 列追加 → 3b backfill
    # → 3c NOT NULL + composite FK) で進めるが、ORM 最終形は NOT NULL。
    source_name: Mapped[SourceName] = mapped_column(SourceNameType, nullable=False)
    status: Mapped[str] = mapped_column(String(20))
    staged_attributes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    leased_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
