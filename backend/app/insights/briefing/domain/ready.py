"""ReadyForBriefing — 1 カテゴリ × 1 週の briefing 実行可能状態の precondition 型。

Trend Discovery の ``ReadyForTrendDiscovery`` と同じ pattern A'
(``feedback_taskiq_basemodel_required.md``, typed-pipeline spec):

- 入口 task (cron) が cron 引数から構築する Ready
- ``model_validator`` で ``week_start.weekday() == 0`` を構造的に保証
- ``try_advance_from`` で「既存 briefing あり → None」の業務正常 skip
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = structlog.get_logger(__name__)


class BriefingExistenceProtocol(Protocol):
    """Briefing Repository の cheap exists 判定 contract。"""

    async def exists(self, *, week_start: date, category_id: int) -> bool: ...


class ReadyForBriefing(BaseModel):
    """1 カテゴリ × 1 週の briefing を実行可能な状態を表す precondition 型。

    Invariants:
    - ``week_start.weekday() == 0`` (JST 月曜)
    - ``category_id > 0``
    - frozen
    """

    model_config = ConfigDict(frozen=True)

    week_start: date
    category_id: int = Field(gt=0)

    @model_validator(mode="after")
    def _ensure_monday(self) -> Self:
        if self.week_start.weekday() != 0:
            raise ValueError(
                f"week_start must be a Monday (JST), got {self.week_start} "
                f"(weekday={self.week_start.weekday()})"
            )
        return self

    @classmethod
    async def try_advance_from(
        cls,
        *,
        week_start: date,
        category_id: int,
        briefing_repo: BriefingExistenceProtocol,
    ) -> ReadyForBriefing | None:
        """Briefing 生成へ advance できるかの判定。

        Precondition:
        - 同 (week, category) の briefing 未生成

        Returns:
            進める場合: ``ReadyForBriefing``
            進めない場合: ``None`` (既存あり、業務正常状態)
        """
        candidate = cls(week_start=week_start, category_id=category_id)
        if await briefing_repo.exists(week_start=week_start, category_id=category_id):
            logger.info(
                "briefing_skipped_existing",
                week_start=week_start.isoformat(),
                category_id=category_id,
            )
            return None
        return candidate
