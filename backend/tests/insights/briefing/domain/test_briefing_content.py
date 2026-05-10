"""WeeklyBriefingContent / BriefingStory のスキーマ + ハルシネーション検証テスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.insights.briefing.domain.briefing import BriefingStory, WeeklyBriefingContent


class TestBriefingStory:
    def test_takeaway_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            BriefingStory(takeaway="", article_ids=[1])

    def test_article_ids_min_length_1(self) -> None:
        with pytest.raises(ValidationError):
            BriefingStory(takeaway="x", article_ids=[])


class TestWeeklyBriefingContent:
    def test_empty_stories_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WeeklyBriefingContent(headline="x", overview="x", stories=[])

    def test_overview_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WeeklyBriefingContent(
                headline="x",
                overview="",
                stories=[BriefingStory(takeaway="t", article_ids=[1])],
            )

    def test_subset_check_passes_when_no_context(self) -> None:
        """context 未指定のときは subset 検証をスキップする (テスト/CLI 経路許容)。"""
        content = WeeklyBriefingContent(
            headline="今週は AI",
            overview="overview narrative",
            stories=[BriefingStory(takeaway="t", article_ids=[42])],
        )
        assert content.stories[0].article_ids == [42]

    def test_subset_check_passes_when_all_ids_in_input(self) -> None:
        data = {
            "headline": "今週は AI",
            "overview": "overview narrative",
            "stories": [{"takeaway": "t", "article_ids": [1, 2]}],
        }
        result = WeeklyBriefingContent.model_validate(
            data, context={"input_ids": {1, 2, 3}}
        )
        assert result.stories[0].article_ids == [1, 2]

    def test_subset_check_rejects_unknown_id(self) -> None:
        """LLM が捏造した article_id を構造的に弾く。"""
        data = {
            "headline": "今週は AI",
            "overview": "overview narrative",
            "stories": [{"takeaway": "t", "article_ids": [1, 999]}],
        }
        with pytest.raises(ValidationError, match="999"):
            WeeklyBriefingContent.model_validate(data, context={"input_ids": {1, 2, 3}})
