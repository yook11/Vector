"""取得済みの事実だけで、トレンドの開始条件を判断する。"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.insights.trend_discovery.domain.ready import (
    ReadyForTrendDiscovery,
    TrendDiscoveryReadyBuildFacts,
    TrendDiscoveryReadyBuildRejectionReason,
)
from app.insights.trend_discovery.domain.trend import TrendWeeks

WEEKS = TrendWeeks(snapshot_date=date(2026, 5, 3))


def test_builds_ready_for_an_ungenerated_week_with_articles():
    """未生成で記事が存在する週だけ、生成入力を返す。"""
    facts = TrendDiscoveryReadyBuildFacts(
        already_generated=False, analyzed_article_count=3
    )
    ready = ReadyForTrendDiscovery.from_facts(weeks=WEEKS, facts=facts)
    assert ready == ReadyForTrendDiscovery(weeks=WEEKS, analyzed_article_count=3)


@pytest.mark.parametrize("count", [None, 0, 3])
def test_already_generated_takes_priority_over_article_count(count):
    """生成済みの週は記事数にかかわらず開始しない。"""
    facts = TrendDiscoveryReadyBuildFacts(
        already_generated=True, analyzed_article_count=count
    )
    assert (
        ReadyForTrendDiscovery.from_facts(weeks=WEEKS, facts=facts)
        is TrendDiscoveryReadyBuildRejectionReason.ALREADY_GENERATED
    )


def test_rejects_an_ungenerated_week_without_articles():
    """未生成でも記事が0件なら開始しない。"""
    facts = TrendDiscoveryReadyBuildFacts(
        already_generated=False, analyzed_article_count=0
    )
    assert (
        ReadyForTrendDiscovery.from_facts(weeks=WEEKS, facts=facts)
        is TrendDiscoveryReadyBuildRejectionReason.NO_ARTICLES
    )


def test_missing_count_for_an_ungenerated_week_is_an_error():
    """未生成の週で件数を取得していないことを、対象なしと誤認しない。"""
    facts = TrendDiscoveryReadyBuildFacts(
        already_generated=False, analyzed_article_count=None
    )
    with pytest.raises(ValueError, match="analyzed_article_count is required"):
        ReadyForTrendDiscovery.from_facts(weeks=WEEKS, facts=facts)


@pytest.mark.parametrize("count", [0, -1])
def test_ready_requires_a_positive_article_count(count):
    """Ready自身も記事数が正であることを保証する。"""
    with pytest.raises(ValidationError):
        ReadyForTrendDiscovery(weeks=WEEKS, analyzed_article_count=count)


def test_ready_is_immutable():
    """開始判定後の記事数を書き換えられない。"""
    ready = ReadyForTrendDiscovery(weeks=WEEKS, analyzed_article_count=3)
    with pytest.raises(ValidationError):
        ready.analyzed_article_count = 0
