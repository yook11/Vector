"""週次ブリーフィングの生成タスクへ渡す、対象の週とカテゴリ。

taskiq の formatter は素の dataclass を扱えず PydanticSerializationError で死ぬため
(Issue #441 / #558)、kiq 引数は Pydantic ``BaseModel(frozen=True)`` にする。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class BriefingTaskInput(BaseModel):
    """``generate_briefing_for_category`` の kiq 引数。"""

    model_config = ConfigDict(frozen=True)

    week_start: date
    category_id: int
