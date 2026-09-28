"""ReadyForBriefing.from_facts の開始条件判定と、素材の記事の制約テスト。"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.insights.briefing.domain.ready import (
    BriefingArticle,
    BriefingReadyBuildFacts,
    BriefingReadyBuildRejectionReason,
    ReadyForBriefing,
)


def _facts(
    *,
    already_generated: bool = False,
    articles: tuple[BriefingArticle, ...] = (
        BriefingArticle(analyzed_article_id=10, translated_title="t", summary="s"),
    ),
) -> BriefingReadyBuildFacts:
    return BriefingReadyBuildFacts(
        category_slug="ai",
        category_name="AI",
        already_generated=already_generated,
        articles=articles,
    )


class TestFromFacts:
    def test_builds_ready_with_category_and_articles(self) -> None:
        article = BriefingArticle(
            analyzed_article_id=10, translated_title="記事", summary="要約"
        )

        ready = ReadyForBriefing.from_facts(
            week_start=date(2026, 4, 20),
            category_id=1,
            facts=_facts(articles=(article,)),
        )

        assert ready == ReadyForBriefing(
            week_start=date(2026, 4, 20),
            category_id=1,
            category_slug="ai",
            category_name="AI",
            articles=(article,),
        )

    def test_rejects_missing_category(self) -> None:
        result = ReadyForBriefing.from_facts(
            week_start=date(2026, 4, 20), category_id=1, facts=None
        )

        assert result is BriefingReadyBuildRejectionReason.CATEGORY_MISSING

    def test_rejects_already_generated_week(self) -> None:
        result = ReadyForBriefing.from_facts(
            week_start=date(2026, 4, 20),
            category_id=1,
            facts=_facts(already_generated=True),
        )

        assert result is BriefingReadyBuildRejectionReason.ALREADY_GENERATED

    def test_rejects_week_without_articles(self) -> None:
        result = ReadyForBriefing.from_facts(
            week_start=date(2026, 4, 20),
            category_id=1,
            facts=_facts(articles=()),
        )

        assert result is BriefingReadyBuildRejectionReason.NO_ARTICLES

    def test_already_generated_wins_over_no_articles(self) -> None:
        """保存済みの週は記事が 0 件でも冪等な skip として扱い、REJECTED にしない。"""
        result = ReadyForBriefing.from_facts(
            week_start=date(2026, 4, 20),
            category_id=1,
            facts=_facts(already_generated=True, articles=()),
        )

        assert result is BriefingReadyBuildRejectionReason.ALREADY_GENERATED

    def test_non_monday_raises_before_business_checks(self) -> None:
        """月曜でない週は、記事 0 件などの判定より先に入力の誤りとして弾く。"""
        # 2026-04-21 is a Tuesday
        with pytest.raises(ValueError, match="Monday"):
            ReadyForBriefing.from_facts(
                week_start=date(2026, 4, 21),
                category_id=1,
                facts=_facts(articles=()),
            )


class TestReadyInvariants:
    def test_non_monday_rejected_on_direct_construction(self) -> None:
        with pytest.raises(ValidationError, match="Monday"):
            ReadyForBriefing(
                week_start=date(2026, 4, 21),
                category_id=1,
                category_slug="ai",
                category_name="AI",
                articles=(
                    BriefingArticle(
                        analyzed_article_id=10, translated_title="t", summary="s"
                    ),
                ),
            )

    def test_requires_at_least_one_article(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForBriefing(
                week_start=date(2026, 4, 20),
                category_id=1,
                category_slug="ai",
                category_name="AI",
                articles=(),
            )


class TestBriefingArticle:
    @pytest.mark.parametrize(
        "fields",
        [
            {"analyzed_article_id": 0, "translated_title": "t", "summary": "s"},
            {"analyzed_article_id": 1, "translated_title": "", "summary": "s"},
            {"analyzed_article_id": 1, "translated_title": "t", "summary": ""},
        ],
    )
    def test_rejects_invalid_fields(self, fields: dict) -> None:
        with pytest.raises(ValidationError):
            BriefingArticle(**fields)

    def test_frozen(self) -> None:
        article = BriefingArticle(
            analyzed_article_id=1, translated_title="t", summary="s"
        )
        with pytest.raises(ValidationError):
            article.summary = "changed"  # type: ignore[misc]
