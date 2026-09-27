"""BriefingTaskInput の制約テスト。"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.queue.messages.briefing import BriefingTaskInput


class TestBriefingTaskInput:
    def test_frozen(self) -> None:
        input_ = BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1)
        with pytest.raises(ValidationError):
            input_.category_id = 2  # type: ignore[misc]

    def test_category_id_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            BriefingTaskInput(week_start=date(2026, 4, 20), category_id=0)
