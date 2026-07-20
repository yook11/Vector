"""ユーザーごとのJST日次agent request予約数。"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["AgentUserDailyQuota"]


class AgentUserDailyQuota(Base):
    """ユーザーごとのJST日次予約カウンター。"""

    __tablename__ = "agent_user_daily_quotas"
    __table_args__ = (
        CheckConstraint(
            "used_count >= 0 AND used_count <= 10",
            name="ck_agent_user_daily_quotas_used_count_range",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "auth.user.id",
            ondelete="CASCADE",
            name="fk_agent_user_daily_quotas_user_id",
        ),
        primary_key=True,
    )
    usage_date: Mapped[date] = mapped_column(Date(), primary_key=True)
    used_count: Mapped[int] = mapped_column(Integer(), nullable=False)
