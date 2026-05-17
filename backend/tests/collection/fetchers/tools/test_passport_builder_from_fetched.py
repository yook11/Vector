"""``try_build_passport`` のユニットテスト (DB 非依存)。

``FetchedArticle`` 入力を passport (Ready / Observed / drop) に変換する
分岐契約を検証する。title / URL / body / published の各境界と、profile の
title policy (``html_preferred`` = 仮タイトル) による Ready gate を網羅し、
private helper ``_build_passport`` の判定順を固定する。

title policy が ``html_preferred`` のとき body + published が揃っても Ready
経路を止め観測事実を全保存する不変は本ファイル固有のケース。
``ObservedArticle`` は補完ポリシーを持たない (policy は per-source = profile)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
)
from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    HTML_TITLE_PROFILE,
    AnalyzableField,
    FieldCompletionPolicy,
    SourceCompletionProfile,
)
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.passport_builder import try_build_passport
from app.shared.value_objects.source_name import SourceName

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


def _call(*, profile: SourceCompletionProfile = DEFAULT_PROFILE, **overrides):
    args = {**_BASE_FETCHED, **overrides}
    return try_build_passport(
        FetchedArticle(**args),
        source_id=1,
        source_name=_SOURCE_NAME,
        origin=ObservedOrigin.feed,
        profile=profile,
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
    """tz-naive datetime は PublishedAt 構造違反 → published 不採用。"""
    naive = datetime(2026, 5, 1, 12, 0)
    result = _call(published_at=naive)
    assert isinstance(result, ObservedArticle)
    assert result.published_at is None


def test_accepts_non_utc_published() -> None:
    jst = timezone(timedelta(hours=9))
    result = _call(published_at=datetime(2026, 5, 1, 21, 0, tzinfo=jst))
    assert isinstance(result, AnalyzableArticle)


@pytest.mark.parametrize("title", ["", "   ", "\n\t  "])
def test_drops_when_title_is_empty(title: str) -> None:
    assert _call(title=title) is None


def test_trims_title_whitespace_and_caps_500_chars() -> None:
    long_title = "  " + ("a" * 600) + "  "
    result = _call(title=long_title)
    assert isinstance(result, AnalyzableArticle)
    assert result.title == "a" * 500


def test_drops_when_url_is_empty() -> None:
    assert _call(url="") is None


def test_drops_when_url_is_private_ip_literal() -> None:
    """SSRF 防御 (SafeUrl): IP リテラルが private/loopback なら drop。"""
    assert _call(url="http://127.0.0.1/secret") is None


def test_drops_when_url_is_not_http_scheme() -> None:
    assert _call(url="javascript:alert(1)") is None


def test_stamps_origin_on_observed_facts() -> None:
    """``origin`` 引数が ``ObservedField.origin`` に stamp される (audit)。"""
    result = try_build_passport(
        FetchedArticle(**{**_BASE_FETCHED, "body": None}),
        source_id=1,
        source_name=_SOURCE_NAME,
        origin=ObservedOrigin.sitemap,
    )
    assert isinstance(result, ObservedArticle)
    assert result.title is not None
    assert result.title.origin is ObservedOrigin.sitemap
    assert result.source_name == _SOURCE_NAME


def test_html_preferred_profile_propagates_observed_facts_when_body_is_none() -> None:
    """title=``html_preferred`` でも観測 title/published は事実として保存される。"""
    result = _call(body=None, profile=HTML_TITLE_PROFILE)
    assert isinstance(result, ObservedArticle)
    assert result.title is not None
    assert result.title.value == _VALID_TITLE


def test_html_preferred_profile_blocks_ready_path_even_when_body_and_published_present() -> (  # noqa: E501
    None
):
    """title=``html_preferred`` (仮タイトル) は body + published が揃っても
    Ready 経路を止めて Observed に落ちる (HTML 補完で title 上書きの安全弁)。"""
    result = _call(profile=HTML_TITLE_PROFILE)
    assert isinstance(result, ObservedArticle)
    assert result.published_at is not None
    assert result.published_at.value.value == _PUBLISHED


def test_any_html_preferred_field_precludes_stage1_ready_even_with_valid_body_and_published() -> (  # noqa: E501
    None
):
    """非 title field の ``html_preferred`` も Ready 経路を止める。

    旧 ``force_html_title`` (title policy 単独 gate) では捕捉できなかった
    一般不変条件を固定する: profile のどこかに ``html_preferred`` があれば
    観測事実だけで品質ゲートを満たしても Stage-1 Ready にしない。実 2
    profile も同一不変条件に従う (R/H byte 不変の証跡)。
    """
    body_html_preferred = SourceCompletionProfile(
        {
            AnalyzableField.title: FieldCompletionPolicy.observed_preferred,
            AnalyzableField.body: FieldCompletionPolicy.html_preferred,
            AnalyzableField.published_at: FieldCompletionPolicy.observed_preferred,
        }
    )
    assert isinstance(_call(profile=body_html_preferred), ObservedArticle)
    assert isinstance(_call(profile=DEFAULT_PROFILE), AnalyzableArticle)
    assert isinstance(_call(profile=HTML_TITLE_PROFILE), ObservedArticle)
