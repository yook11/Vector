"""taskiq subtask 引数の VO (BaseModel frozen)。

taskiq の formatter は素の dataclass を扱えず PydanticSerializationError で死ぬ
ため (Issue #441 / #558)、kiq 引数は必ず Pydantic ``BaseModel(frozen=True)``
を採る (`feedback_taskiq_basemodel_required.md`)。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class BriefingTaskInput(BaseModel):
    """``generate_briefing_for_category`` の kiq 引数。"""

    model_config = ConfigDict(frozen=True)

    week_start: date
    category_id: int = Field(gt=0)
