"""取得済みの事実だけで、トレンドの開始条件を判断する。"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.insights.trend_discovery.domain.ready import (
    ReadyForTrendDiscovery,
    TrendDiscoveryReadyBuildFacts,
    TrendDiscoveryReadyBuildRejectionReason,
)
from app.insights.trend_discovery.domain.trend import TrendWindow

WINDOW = TrendWindow(window_end=date(2026, 5, 3))


def test_builds_ready_for_an_ungenerated_window_with_articles():
    """未生成で記事が存在する期間だけ、生成入力を返す。"""
    facts = TrendDiscoveryReadyBuildFacts(
        already_generated=False, source_analysis_count=3
    )
    ready = ReadyForTrendDiscovery.from_facts(window=WINDOW, facts=facts)
    assert ready == ReadyForTrendDiscovery(window=WINDOW, source_analysis_count=3)


@pytest.mark.parametrize("count", [None, 0, 3])
def test_already_generated_takes_priority_over_article_count(count):
    """生成済みの期間は記事数にかかわらず開始しない。"""
    facts = TrendDiscoveryReadyBuildFacts(
        already_generated=True, source_analysis_count=count
    )
    assert (
        ReadyForTrendDiscovery.from_facts(window=WINDOW, facts=facts)
        is TrendDiscoveryReadyBuildRejectionReason.ALREADY_GENERATED
    )


def test_rejects_an_ungenerated_window_without_articles():
    """未生成でも記事が0件なら開始しない。"""
    facts = TrendDiscoveryReadyBuildFacts(
        already_generated=False, source_analysis_count=0
    )
    assert (
        ReadyForTrendDiscovery.from_facts(window=WINDOW, facts=facts)
        is TrendDiscoveryReadyBuildRejectionReason.NO_ARTICLES
    )


def test_missing_count_for_an_ungenerated_window_is_an_error():
    """未生成期間の件数未取得を、対象なしと誤認しない。"""
    facts = TrendDiscoveryReadyBuildFacts(
        already_generated=False, source_analysis_count=None
    )
    with pytest.raises(ValueError, match="source_analysis_count is required"):
        ReadyForTrendDiscovery.from_facts(window=WINDOW, facts=facts)


@pytest.mark.parametrize("count", [0, -1])
def test_ready_requires_a_positive_source_count(count):
    """Ready自身も集計元記事数が正であることを保証する。"""
    with pytest.raises(ValidationError):
        ReadyForTrendDiscovery(window=WINDOW, source_analysis_count=count)


def test_ready_is_immutable():
    """開始判定後の集計元記事数を書き換えられない。"""
    ready = ReadyForTrendDiscovery(window=WINDOW, source_analysis_count=3)
    with pytest.raises(ValidationError):
        ready.source_analysis_count = 0
