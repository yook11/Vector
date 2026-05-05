"""``article_urls`` テーブル — URL identity 台帳。

PR2.5-A 新設。Pattern R / Pattern H の両経路で URL の一意性を物理的に保証する
SSoT。``normalized_url`` の UNIQUE 制約だけで cross-table dedup
(articles と pending_html_articles の重複) が成立する。

設計詳細は ``specs/pipeline-events-stage2-design.md`` の §データフロー §スキーマ案。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import SafeUrlType
from app.shared.value_objects.safe_url import SafeUrl


class ArticleUrl(Base):
    """記事 URL の identity 台帳 (不変、永続)。

    - ``normalized_url`` の UNIQUE が dedup の SSoT
    - ``original_url`` は表示用、dedup には使わない
    - ``first_seen_*`` は監査情報 (どのソースが最初にこの URL を見たか)

    削除は基本的に行わない (URL は永続)。news_source 削除時は RESTRICT で
    遮断 (履歴を守る、運用が意識的に対応する)。
    """

    __tablename__ = "article_urls"
    __table_args__ = (
        UniqueConstraint("normalized_url", name="uq_article_urls_normalized_url"),
        CheckConstraint(
            "normalized_url ~ '^https?://.+'",
            name="ck_article_urls_normalized_url_scheme",
        ),
        CheckConstraint(
            "original_url ~ '^https?://.+'",
            name="ck_article_urls_original_url_scheme",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    normalized_url: Mapped[SafeUrl] = mapped_column(SafeUrlType)
    original_url: Mapped[SafeUrl] = mapped_column(SafeUrlType)
    first_seen_source_id: Mapped[int] = mapped_column(
        ForeignKey("news_sources.id", ondelete="RESTRICT"),
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
