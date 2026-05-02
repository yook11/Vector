"""ReadyForBriefing.try_advance_from の precondition / week 検証テスト。"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.insights.briefing.domain.ready import ReadyForBriefing


class FakeRepo:
    def __init__(self, exists: bool) -> None:
        self._exists = exists
        self.exists = AsyncMock(return_value=exists)


class TestEnsureMonday:
    def test_monday_accepted(self) -> None:
        # 2026-04-20 is a Monday (JST)
        ready = ReadyForBriefing(week_start=date(2026, 4, 20), category_id=1)
        assert ready.week_start.weekday() == 0

    def test_non_monday_rejected(self) -> None:
        # 2026-04-21 is a Tuesday
        with pytest.raises(ValidationError, match="Monday"):
            ReadyForBriefing(week_start=date(2026, 4, 21), category_id=1)


class TestTryAdvanceFrom:
    @pytest.mark.asyncio
    async def test_returns_ready_when_no_existing(self) -> None:
        repo = FakeRepo(exists=False)
        ready = await ReadyForBriefing.try_advance_from(
            week_start=date(2026, 4, 20),
            category_id=1,
            force=False,
            briefing_repo=repo,
        )
        assert ready is not None
        assert ready.week_start == date(2026, 4, 20)
        assert ready.category_id == 1
        assert ready.force is False

    @pytest.mark.asyncio
    async def test_returns_none_when_existing_and_not_forced(self) -> None:
        repo = FakeRepo(exists=True)
        ready = await ReadyForBriefing.try_advance_from(
            week_start=date(2026, 4, 20),
            category_id=1,
            force=False,
            briefing_repo=repo,
        )
        assert ready is None

    @pytest.mark.asyncio
    async def test_returns_ready_when_forced_and_existing(self) -> None:
        repo = FakeRepo(exists=True)
        ready = await ReadyForBriefing.try_advance_from(
            week_start=date(2026, 4, 20),
            category_id=1,
            force=True,
            briefing_repo=repo,
        )
        assert ready is not None
        assert ready.force is True
        # force=True の経路では exists を呼ばないことを確認
        repo.exists.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_monday_raises_before_repo_call(self) -> None:
        """try_advance_from でも weekday 検証が exists 呼出より先に発火する。"""
        repo = FakeRepo(exists=False)
        with pytest.raises(ValidationError):
            await ReadyForBriefing.try_advance_from(
                week_start=date(2026, 4, 21),
                category_id=1,
                force=False,
                briefing_repo=repo,
            )
        repo.exists.assert_not_awaited()
