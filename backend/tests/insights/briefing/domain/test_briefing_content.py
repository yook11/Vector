"""WeeklyBriefingContent / KeyArticle / WatchPoint のスキーマ + 検証テスト。

不変条件の正本 (一次防御):
- key_articles / watch_points の件数・文字列長 (F10 構造防御)
- key_articles[].article_id の重複拒否 (UI の React key 一意性)
- key_articles[].article_id ⊆ input_ids (ハルシネーション検出、context 付き)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.insights.briefing.domain.briefing import (
    MAX_KEY_ARTICLE_SIGNIFICANCE_LEN,
    MAX_KEY_ARTICLES_PER_BRIEFING,
    MAX_WATCH_POINT_STATEMENT_LEN,
    MAX_WATCH_POINTS_PER_BRIEFING,
    KeyArticle,
    WatchPoint,
    WeeklyBriefingContent,
)


def _content(**overrides: object) -> dict[str, object]:
    """正常系の最小 payload。各テストで壊したい field だけ上書きする。"""
    base: dict[str, object] = {
        "headline": "今週は AI",
        "overview": "overview narrative",
        "key_articles": [{"article_id": 1, "significance": "なぜ重要か"}],
        "watch_points": [{"statement": "今後どこを見るべきか"}],
    }
    base.update(overrides)
    return base


class TestKeyArticle:
    def test_significance_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            KeyArticle(article_id=1, significance="")


class TestWatchPoint:
    def test_statement_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WatchPoint(statement="")


class TestWeeklyBriefingContentNormalPath:
    def test_accepts_minimal_valid_content(self) -> None:
        content = WeeklyBriefingContent.model_validate(_content())
        assert content.key_articles[0].article_id == 1
        assert content.key_articles[0].significance == "なぜ重要か"
        assert content.watch_points[0].statement == "今後どこを見るべきか"

    def test_overview_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(overview=""))


class TestKeyArticlesInvariants:
    def test_rejects_empty_key_articles(self) -> None:
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(key_articles=[]))

    def test_rejects_abnormal_key_article_count(self) -> None:
        """F10 異常検知ライン超 (injection / 暴走疑い) を弾く。

        editorial 上限 (プロンプトの「最大 5 件」) ではなく、structural な
        ``MAX_KEY_ARTICLES_PER_BRIEFING`` 超を境界とする。
        """
        too_many = [
            {"article_id": i, "significance": f"s{i}"}
            for i in range(MAX_KEY_ARTICLES_PER_BRIEFING + 1)
        ]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(key_articles=too_many))

    def test_rejects_duplicate_key_article_ids(self) -> None:
        """同一 article_id の重複は件数と独立に常時拒否する。"""
        dup = [
            {"article_id": 7, "significance": "a"},
            {"article_id": 7, "significance": "b"},
        ]
        with pytest.raises(ValidationError, match="duplicate"):
            WeeklyBriefingContent.model_validate(_content(key_articles=dup))

    def test_rejects_oversize_significance(self) -> None:
        oversize = [
            {
                "article_id": 1,
                "significance": "x" * (MAX_KEY_ARTICLE_SIGNIFICANCE_LEN + 1),
            }
        ]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(key_articles=oversize))


class TestWatchPointsInvariants:
    def test_rejects_empty_watch_points(self) -> None:
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(watch_points=[]))

    def test_rejects_more_than_max_watch_points(self) -> None:
        too_many = [
            {"statement": f"w{i}"} for i in range(MAX_WATCH_POINTS_PER_BRIEFING + 1)
        ]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(watch_points=too_many))

    def test_rejects_oversize_statement(self) -> None:
        oversize = [{"statement": "x" * (MAX_WATCH_POINT_STATEMENT_LEN + 1)}]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(watch_points=oversize))


class TestArticleIdSubsetCheck:
    def test_skips_subset_check_without_context(self) -> None:
        """context 未指定のときは subset 検証をスキップする (テスト/CLI 経路許容)。"""
        content = WeeklyBriefingContent.model_validate(
            _content(key_articles=[{"article_id": 42, "significance": "s"}])
        )
        assert content.key_articles[0].article_id == 42

    def test_subset_check_passes_when_all_ids_in_input(self) -> None:
        data = _content(
            key_articles=[
                {"article_id": 1, "significance": "a"},
                {"article_id": 2, "significance": "b"},
            ]
        )
        result = WeeklyBriefingContent.model_validate(
            data, context={"input_ids": {1, 2, 3}}
        )
        assert [ka.article_id for ka in result.key_articles] == [1, 2]

    def test_rejects_key_article_id_not_in_input_ids(self) -> None:
        """LLM が捏造した article_id を構造的に弾く。"""
        data = _content(
            key_articles=[
                {"article_id": 1, "significance": "a"},
                {"article_id": 999, "significance": "b"},
            ]
        )
        with pytest.raises(ValidationError, match="999"):
            WeeklyBriefingContent.model_validate(data, context={"input_ids": {1, 2, 3}})
