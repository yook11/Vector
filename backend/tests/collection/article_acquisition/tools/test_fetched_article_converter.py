"""``convert_fetched_article`` のユニットテスト (DB 非依存)。

``FetchedArticle`` 入力を ``AnalyzableArticle`` / ``ObservedArticle`` / 棄却
(``ConversionRejection``) に変換する分岐契約を検証する。title / URL / body /
published の各境界と、profile の title policy (``html_preferred`` = 仮タイトル)
による Ready gate を網羅し、``convert_fetched_article`` の判定順を固定する。

convert は想定内に total: 変換不能 entry は raise でなく ``ConversionRejection``
値で返し、握りつぶさず理由付きで表に出す。棄却値は責任元 VO の reason を
``outcome_code`` に verbatim で運ぶ (URL は ``SafeUrlInvalidReason``、title 欠落 /
想定外バグは acquisition 所有の ``AcquisitionConversionDefect``)。想定外 bug の
値化 funnel ``unexpected_rejection`` の契約 (UNEXPECTED_ERROR + ``cause`` 保持) も
併せて固定する。Ready の Pydantic 失敗 / tz-naive published の Observed fallback
は **結果不変** (byte 等価) であることを引き続き固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import ClassVar

import pytest

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetched_article_converter import (
    ConversionRejection,
    convert_fetched_article,
    unexpected_rejection,
)
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
)
from app.collection.domain.canonical_article_url import (
    CanonicalArticleUrlInvalidError,
)
from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
    CompletableField,
    FieldCompletionRule,
)
from app.collection.sources.article_source import ArticleSource
from app.collection.sources.base_article_source import BaseArticleSource
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
) -> ArticleSource:
    """``convert_fetched_article`` が読む 3 属性を持つ fake Source。

    ``read`` / ``map_entry`` は本変換器からは呼ばれないが ``ArticleSource`` を
    構造的に満たすため no-op を置く (in_scope/select は ``BaseArticleSource``)。
    """

    class _FakeSource(BaseArticleSource):
        name: ClassVar[SourceName] = _SOURCE_NAME
        endpoint_url: ClassVar[str] = "https://example.test/feed"
        observed_origin: ClassVar[ObservedOrigin] = origin
        completion_policy: ClassVar[ArticleCompletionPolicy] = profile

        @classmethod
        async def read(cls, tools: ReaderTools) -> list[FetchedArticle]:  # noqa: ARG003
            return []

        @classmethod
        def map_entry(cls, entry: FetchedArticle) -> FetchedArticle:
            return entry

    return _FakeSource


def _call(*, profile: ArticleCompletionPolicy = DEFAULT_POLICY, **overrides):
    args = {**_BASE_FETCHED, **overrides}
    return convert_fetched_article(
        FetchedArticle(**args),
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


@pytest.mark.parametrize("title", ["", "   ", "\n\t  "])
def test_rejects_missing_title_when_title_is_empty(title: str) -> None:
    result = _call(title=title)
    assert isinstance(result, ConversionRejection)
    assert result.outcome_code == "acquisition_conversion_title_missing"
    assert result.cause is None  # acquisition 方針違反であり VO 例外ではない


def test_trims_title_whitespace_and_caps_500_chars() -> None:
    long_title = "  " + ("a" * 600) + "  "
    result = _call(title=long_title)
    assert isinstance(result, AnalyzableArticle)
    assert result.title == "a" * 500


def test_rejects_empty_url_as_url_empty() -> None:
    """空 URL は SafeUrl の ``url_empty`` を verbatim で運ぶ (責任元 = URL VO)。"""
    result = _call(url="")
    assert isinstance(result, ConversionRejection)
    assert result.outcome_code == "url_empty"
    assert isinstance(result.cause, CanonicalArticleUrlInvalidError)


def test_rejects_private_ip_url_as_host_not_public_ip() -> None:
    """SSRF 防御 (SafeUrl): private/loopback IP は ``host_not_public_ip``。"""
    result = _call(url="http://127.0.0.1/secret")
    assert isinstance(result, ConversionRejection)
    assert result.outcome_code == "host_not_public_ip"


def test_rejects_non_http_url_as_url_not_http() -> None:
    result = _call(url="javascript:alert(1)")
    assert isinstance(result, ConversionRejection)
    assert result.outcome_code == "url_not_http"


def test_rejects_overlong_url_as_url_too_long() -> None:
    """2048 字超の URL は ``url_too_long`` に精密分類される (旧 INVALID_URL 潰し)。"""
    result = _call(url="https://example.com/" + "a" * 2050)
    assert isinstance(result, ConversionRejection)
    assert result.outcome_code == "url_too_long"


def test_rejection_carries_structured_observation_fields() -> None:
    """棄却値は FetchedArticle の観測スナップショットを構造化保持する。"""
    result = _call(url="javascript:alert(1)", body=_VALID_BODY)
    assert isinstance(result, ConversionRejection)
    assert result.outcome_code == "url_not_http"
    assert result.source_name == str(_SOURCE_NAME)
    assert result.raw_url == "javascript:alert(1)"
    assert result.has_title is True
    assert result.body_length == len(_VALID_BODY)
    assert result.has_published_at is True


def test_empty_url_rejection_reports_absent_raw_url() -> None:
    result = _call(url="")
    assert isinstance(result, ConversionRejection)
    assert result.raw_url is None


def test_invalid_url_rejection_carries_url_invalid_cause() -> None:
    """URL VO の例外を ``cause`` に保持し、その下に SafeUrl 由来 (ValueError) を
    連鎖する (監査が error_chain を深さ>1 で辿れる)。"""
    result = _call(url="http://127.0.0.1/secret")
    assert isinstance(result, ConversionRejection)
    assert isinstance(result.cause, CanonicalArticleUrlInvalidError)
    assert isinstance(result.cause.__cause__, ValueError)


def test_stamps_origin_on_observed_facts() -> None:
    """``observed_origin`` が ``ObservedField.origin`` に stamp される (audit)。"""
    result = convert_fetched_article(
        FetchedArticle(**{**_BASE_FETCHED, "body": None}),
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


# ── 想定外 bug の値化 funnel ``unexpected_rejection`` ───────────────────────


def test_unexpected_rejection_funnels_to_unexpected_error_reason() -> None:
    """想定外 bug は ``UNEXPECTED_ERROR`` の ``ConversionRejection`` に値化される。"""
    result = unexpected_rejection(
        FetchedArticle(**_BASE_FETCHED),
        source=_source(),
        cause=RuntimeError("post-precondition invariant violation"),
    )
    assert isinstance(result, ConversionRejection)
    assert result.outcome_code == "acquisition_conversion_unexpected_error"


def test_unexpected_rejection_keeps_origin_cause() -> None:
    """原因例外を ``cause`` に保持し監査が本当のバグの FQN / chain を辿れる。"""
    cause = RuntimeError("boom")
    result = unexpected_rejection(
        FetchedArticle(**_BASE_FETCHED), source=_source(), cause=cause
    )
    assert result.cause is cause


def test_unexpected_rejection_carries_structured_observation_fields() -> None:
    """観測スナップショット (source_name / raw_url / has_title 等) を構造化保持する。"""
    result = unexpected_rejection(
        FetchedArticle(**_BASE_FETCHED), source=_source(), cause=RuntimeError("x")
    )
    assert result.source_name == str(_SOURCE_NAME)
    assert result.raw_url == _VALID_URL
    assert result.has_title is True
    assert result.body_length == len(_VALID_BODY)
    assert result.has_published_at is True
