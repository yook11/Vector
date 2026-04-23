from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.analysis.domain.value_objects.topic import TopicName
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.category import Category


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("name", "category_id", name="uq_topics_name_category_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[TopicName] = mapped_column()
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), index=True
    )
    # シードトピックは手動キュレーション、AI 動的生成は AI が日本語ラベルも同時出力。
    # DB 物理制約は再分析完了後の rev_H で NOT NULL を付与する。
    label_ja: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    category: Mapped[Category] = relationship(back_populates="topics")
