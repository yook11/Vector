"""新経路の統合・記事構築と拒否情報の伝播。"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.collection.article_completion.content import ScrapedContent
from app.collection.article_completion.errors import ArticleCompletionRejectedError
from app.collection.article_completion.html_completion import complete_with_html
from app.collection.domain.analyzable_article import (
    AnalyzableArticle,
    AnalyzableArticleDefect,
    QualityTooLow,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.source_name import SourceName

_URL = CanonicalArticleUrl("https://example.com/article")
_BODY = (
    "研究チームは新しい観測装置による測定結果を公開した。"
    "得られたデータは次回の実験計画を改善するために利用される。"
)
_PUBLISHED = PublishedAt(datetime(2026, 4, 30, 12, tzinfo=UTC))


@pytest.fixture
def observed() -> ObservedArticle:
    """DB状態を含まない取得済み情報。"""
    return ObservedArticle(
        source_name=SourceName("Feed Source"),
        source_url=_URL,
        title=ObservedField(value="Feed Title", origin=ObservedOrigin.feed),
        body=ObservedField(value="Feed summary", origin=ObservedOrigin.feed),
        published_at=ObservedField(value=_PUBLISHED, origin=ObservedOrigin.feed),
    )


@pytest.mark.parametrize(
    ("policy", "html_title", "expected_title"),
    [
        (DEFAULT_POLICY, None, "Feed Title"),
        (HTML_TITLE_POLICY, "HTML Title", "HTML Title"),
    ],
)
def test_builds_article_using_completion_policy(
    observed: ObservedArticle,
    policy: ArticleCompletionPolicy,
    html_title: str | None,
    expected_title: str,
) -> None:
    """素材を実ポリシーで統合し、その採用値から完成記事を構築する。"""
    html = ScrapedContent(title=html_title, body=_BODY, published_at=None)
    result = complete_with_html(observed, policy, html, source_id=7, source_url=_URL)
    assert result == AnalyzableArticle(
        title=expected_title,
        body=_BODY,
        published_at=_PUBLISHED,
        source_id=7,
        source_url=_URL,
    )


def test_known_build_rejection_preserves_defect() -> None:
    """実際の構築拒否の理由を新エラーへ渡す。"""
    observed = ObservedArticle(
        source_name=SourceName("Feed Source"),
        source_url=_URL,
    )
    html = ScrapedContent(title="Title", body=_BODY, published_at=None)
    with pytest.raises(ArticleCompletionRejectedError) as caught:
        complete_with_html(observed, DEFAULT_POLICY, html, source_id=7, source_url=_URL)
    assert caught.value.defects == (AnalyzableArticleDefect.PUBLISHED_AT_MISSING,)
    assert caught.value.unmapped == ()


def test_rejection_preserves_all_defects_and_unmapped(
    observed: ObservedArticle,
) -> None:
    """構築結果の複数理由と未分類詳細を順序や重複も含めて保持する。"""
    quality = QualityTooLow(
        defects=(
            AnalyzableArticleDefect.UNMAPPED_VALIDATION_ERROR,
            AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
            AnalyzableArticleDefect.UNMAPPED_VALIDATION_ERROR,
        ),
        unmapped=(
            "title:unexpected_title_validation",
            "body:unexpected_body_validation",
        ),
    )
    html = ScrapedContent(title="Title", body=_BODY, published_at=None)
    with (
        patch.object(AnalyzableArticle, "build_or_reject", return_value=quality),
        pytest.raises(ArticleCompletionRejectedError) as caught,
    ):
        complete_with_html(observed, DEFAULT_POLICY, html, source_id=7, source_url=_URL)
    assert caught.value.defects == quality.defects
    assert caught.value.unmapped == quality.unmapped


@pytest.mark.parametrize(
    ("owner", "method"),
    [(ArticleCompletionPolicy, "resolve"), (AnalyzableArticle, "build_or_reject")],
)
def test_unexpected_exception_propagates(
    observed: ObservedArticle, owner, method
) -> None:
    """統合・構築の想定外例外は構築拒否に変換せず伝播させる。"""
    original = RuntimeError("unexpected completion failure")
    html = ScrapedContent(title="Title", body=_BODY, published_at=None)
    with (
        patch.object(owner, method, side_effect=original),
        pytest.raises(RuntimeError) as caught,
    ):
        complete_with_html(observed, DEFAULT_POLICY, html, source_id=7, source_url=_URL)
    assert caught.value is original
