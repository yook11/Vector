"""``convert_fetched_article`` のユニットテスト (DB 非依存)。

受け付け済みの ``FetchedArticle`` を ``AnalyzableArticle`` / ``ObservedArticle`` に
変換する分岐契約を検証する。body / published の各境界と、profile の title policy
(``html_preferred`` = 仮タイトル) による Ready gate を網羅する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import ClassVar

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetched_article_converter import (
    convert_fetched_article,
)
from app.collection.article_acquisition.raw_article import RawArticle
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
)
from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
    CompletableField,
    FieldCompletionRule,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.source_metadata import SourceMetadata
from app.collection.sources.source_name import SourceName

_PUBLISHED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_VALID_URL = "https://example.com/articles/hello-world"
_VALID_TITLE = "Hello World"
_VALID_BODY = "x" * ARTICLE_BODY_MIN_LENGTH
_SOURCE_NAME = SourceName("Example")

_BASE_FETCHED: dict = {
    "title": _VALID_TITLE,
    "url": _VALID_URL,
    "body": _VALID_BODY,
    "published_at": _PUBLISHED,
}


def _source(
    *,
    origin: ObservedOrigin = ObservedOrigin.feed,
    profile: ArticleCompletionPolicy = DEFAULT_POLICY,
) -> SourceMetadata:
    """変換に必要なソース情報だけを公開する。"""

    class _FakeSource:
        name: ClassVar[SourceName] = _SOURCE_NAME
        observed_origin: ClassVar[ObservedOrigin] = origin
        completion_policy: ClassVar[ArticleCompletionPolicy] = profile
        fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    return _FakeSource


def _call(*, profile: ArticleCompletionPolicy = DEFAULT_POLICY, **overrides):
    args = {**_BASE_FETCHED, **overrides}
    return convert_fetched_article(
        FetchedArticle.from_raw(RawArticle(**args)),
        source=_source(profile=profile),
        source_id=1,
    )


def test_returns_ready_when_body_and_published_present() -> None:
    result = _call()
    assert isinstance(result, AnalyzableArticle)
    assert result.body == _VALID_BODY
    assert result.published_at.value == _PUBLISHED


def test_returns_observed_when_body_is_none() -> None:
    """RSS body 不信用 (旧 Pattern H 相当) を表現する経路。"""
    result = _call(body=None)
    assert isinstance(result, ObservedArticle)
    assert result.body is None  # 取れなかった事実は None
    assert result.published_at is not None
    assert result.published_at.value.value == _PUBLISHED
    assert result.published_at.origin is ObservedOrigin.feed


def test_returns_observed_when_body_too_short() -> None:
    """body が短すぎて Ready 不可でも、観測 body 事実は保存する。"""
    short = "x" * (ARTICLE_BODY_MIN_LENGTH - 1)
    result = _call(body=short)
    assert isinstance(result, ObservedArticle)
    assert result.body is not None
    assert result.body.value == short


def test_returns_observed_when_body_exceeds_max_length() -> None:
    result = _call(body="x" * (ARTICLE_BODY_MAX_LENGTH + 1))
    assert isinstance(result, ObservedArticle)


def test_returns_observed_when_published_missing() -> None:
    result = _call(published_at=None)
    assert isinstance(result, ObservedArticle)
    assert result.published_at is None


def test_drops_naive_published_silently_and_falls_back_to_observed() -> None:
    """tz-naive datetime は PublishedAt 構造違反 → published 不採用 (byte 不変)。"""
    naive = datetime(2026, 5, 1, 12, 0)
    result = _call(published_at=naive)
    assert isinstance(result, ObservedArticle)
    assert result.published_at is None


def test_accepts_non_utc_published() -> None:
    jst = timezone(timedelta(hours=9))
    result = _call(published_at=datetime(2026, 5, 1, 21, 0, tzinfo=jst))
    assert isinstance(result, AnalyzableArticle)


def test_stamps_origin_on_observed_facts() -> None:
    """``observed_origin`` が ``ObservedField.origin`` に stamp される (audit)。"""
    result = convert_fetched_article(
        FetchedArticle.from_raw(RawArticle(**{**_BASE_FETCHED, "body": None})),
        source=_source(origin=ObservedOrigin.sitemap),
        source_id=1,
    )
    assert isinstance(result, ObservedArticle)
    assert result.title is not None
    assert result.title.origin is ObservedOrigin.sitemap
    assert result.source_name == _SOURCE_NAME


def test_html_preferred_profile_propagates_observed_facts_when_body_is_none() -> None:
    """title=``html_preferred`` でも観測 title/published は事実として保存される。"""
    result = _call(body=None, profile=HTML_TITLE_POLICY)
    assert isinstance(result, ObservedArticle)
    assert result.title is not None
    assert result.title.value == _VALID_TITLE


def test_html_preferred_profile_blocks_ready_path_even_when_body_and_published_present() -> (  # noqa: E501
    None
):
    """title=``html_preferred`` (仮タイトル) は body + published が揃っても
    Ready 経路を止めて Observed に落ちる (HTML 補完で title 上書きの安全弁)。"""
    result = _call(profile=HTML_TITLE_POLICY)
    assert isinstance(result, ObservedArticle)
    assert result.published_at is not None
    assert result.published_at.value.value == _PUBLISHED


def test_any_html_preferred_field_requires_html_completion_even_with_valid_body_and_published() -> (  # noqa: E501
    None
):
    """非 title field の ``html_preferred`` も Ready 経路を止める。

    旧 ``force_html_title`` (title policy 単独 gate) では捕捉できなかった
    一般不変条件を固定する: profile のどこかに ``html_preferred`` があれば
    観測事実だけで品質ゲートを満たしても Stage-1 Ready にしない。実 2
    profile も同一不変条件に従う (R/H byte 不変の証跡)。
    """
    body_html_preferred = ArticleCompletionPolicy(
        {
            CompletableField.title: FieldCompletionRule.observed_preferred,
            CompletableField.body: FieldCompletionRule.html_preferred,
            CompletableField.published_at: FieldCompletionRule.observed_preferred,
        }
    )
    assert isinstance(_call(profile=body_html_preferred), ObservedArticle)
    assert isinstance(_call(profile=DEFAULT_POLICY), AnalyzableArticle)
    assert isinstance(_call(profile=HTML_TITLE_POLICY), ObservedArticle)
