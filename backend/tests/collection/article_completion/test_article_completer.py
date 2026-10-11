"""実モデルと実ポリシーを使い、DB・外部通信なしで補完器が観測値と抽出結果から記事を完成させる契約を検証する。"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.collection.article_completion.article_completer import (
    ExtractedContent,
    complete_article,
)
from app.collection.article_completion.errors import ArticleBuildRejectedError
from app.collection.domain.analyzable_article import (
    AnalyzableArticle,
    AnalyzableArticleDefect,
    QualityTooLow,
)
from app.collection.domain.article_url import ArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    HTML_TITLE_POLICY,
)
from app.collection.sources.source_name import SourceName

_URL = ArticleUrl("https://example.com/article")
_OBSERVED_BODY = (
    "フィードの要約では、観測装置の測定結果が公開されたことだけを伝えている。"
    "詳細な数値や今後の計画は記事のページで説明されている。"
)
_EXTRACTED_BODY = (
    "研究チームは新しい観測装置による測定結果を公開した。"
    "得られたデータは次回の実験計画を改善するために利用される。"
)
_OBSERVED_PUBLISHED = PublishedAt(datetime(2026, 4, 30, tzinfo=UTC))
_EXTRACTED_PUBLISHED = PublishedAt(datetime(2026, 5, 1, tzinfo=UTC))


def _observed(
    *,
    title: str = "観測したタイトル",
    body: str | None = None,
    published_at: PublishedAt | None = _OBSERVED_PUBLISHED,
) -> ObservedArticle:
    return ObservedArticle(
        source_name=SourceName("Example"),
        source_url=_URL,
        title=ObservedField(value=title, origin=ObservedOrigin.feed),
        body=(
            ObservedField(value=body, origin=ObservedOrigin.feed)
            if body is not None
            else None
        ),
        published_at=(
            ObservedField(value=published_at, origin=ObservedOrigin.feed)
            if published_at is not None
            else None
        ),
    )


def test_html_title_policy_completes_with_extracted_title() -> None:
    """タイトルをHTML優先とするソースでは、観測と抽出の両方にタイトルがあれば抽出タイトルで完成する。"""
    result = complete_article(
        _observed(title="仮のタイトル"),
        ExtractedContent(
            title="記事ページのタイトル", body=_EXTRACTED_BODY, published_at=None
        ),
        HTML_TITLE_POLICY,
        source_id=7,
    )
    assert result == AnalyzableArticle(
        title="記事ページのタイトル",
        body=_EXTRACTED_BODY,
        published_at=_OBSERVED_PUBLISHED,
        source_id=7,
        source_url=_URL,
    )


def test_html_title_policy_falls_back_to_observed_title() -> None:
    """タイトルをHTML優先とするソースでも、タイトルを抽出できなければ観測タイトルで完成する。"""
    result = complete_article(
        _observed(title="仮のタイトル"),
        ExtractedContent(title=None, body=_EXTRACTED_BODY, published_at=None),
        HTML_TITLE_POLICY,
        source_id=7,
    )
    assert result == AnalyzableArticle(
        title="仮のタイトル",
        body=_EXTRACTED_BODY,
        published_at=_OBSERVED_PUBLISHED,
        source_id=7,
        source_url=_URL,
    )


def test_default_policy_completes_with_observed_title() -> None:
    """既定のソースでは、観測と抽出の両方にタイトルがあれば観測タイトルで完成する。"""
    result = complete_article(
        _observed(title="フィードのタイトル"),
        ExtractedContent(
            title="記事ページのタイトル", body=_EXTRACTED_BODY, published_at=None
        ),
        DEFAULT_POLICY,
        source_id=7,
    )
    assert result == AnalyzableArticle(
        title="フィードのタイトル",
        body=_EXTRACTED_BODY,
        published_at=_OBSERVED_PUBLISHED,
        source_id=7,
        source_url=_URL,
    )


def test_html_required_body_completes_with_extracted_body() -> None:
    """本文をHTML必須とするソースでは、観測本文があっても抽出本文で完成する。"""
    result = complete_article(
        _observed(body=_OBSERVED_BODY),
        ExtractedContent(title=None, body=_EXTRACTED_BODY, published_at=None),
        DEFAULT_POLICY,
        source_id=7,
    )
    assert result == AnalyzableArticle(
        title="観測したタイトル",
        body=_EXTRACTED_BODY,
        published_at=_OBSERVED_PUBLISHED,
        source_id=7,
        source_url=_URL,
    )


def test_html_required_body_rejects_without_extracted_body() -> None:
    """本文をHTML必須とするソースで本文を抽出できなければ、観測本文に戻らず本文なしで拒否する。"""
    with pytest.raises(ArticleBuildRejectedError) as caught:
        complete_article(
            _observed(body=_OBSERVED_BODY),
            ExtractedContent(title=None, body=None, published_at=None),
            DEFAULT_POLICY,
            source_id=7,
        )
    assert caught.value.rejection == QualityTooLow(
        defects=(AnalyzableArticleDefect.BODY_MISSING,)
    )


def test_observed_published_at_takes_priority() -> None:
    """観測と抽出の両方に公開日時があれば、観測の公開日時で完成する。"""
    result = complete_article(
        _observed(published_at=_OBSERVED_PUBLISHED),
        ExtractedContent(
            title=None, body=_EXTRACTED_BODY, published_at=_EXTRACTED_PUBLISHED
        ),
        DEFAULT_POLICY,
        source_id=7,
    )
    assert result == AnalyzableArticle(
        title="観測したタイトル",
        body=_EXTRACTED_BODY,
        published_at=_OBSERVED_PUBLISHED,
        source_id=7,
        source_url=_URL,
    )


def test_extracted_published_at_fills_missing_observation() -> None:
    """観測に公開日時がなければ、抽出した公開日時で完成する。"""
    result = complete_article(
        _observed(published_at=None),
        ExtractedContent(
            title=None, body=_EXTRACTED_BODY, published_at=_EXTRACTED_PUBLISHED
        ),
        DEFAULT_POLICY,
        source_id=7,
    )
    assert result == AnalyzableArticle(
        title="観測したタイトル",
        body=_EXTRACTED_BODY,
        published_at=_EXTRACTED_PUBLISHED,
        source_id=7,
        source_url=_URL,
    )


def test_rejects_when_published_at_is_missing_everywhere() -> None:
    """観測と抽出のどちらにも公開日時がなければ、公開日時なしの構築拒否を例外に持たせて伝える。"""
    with pytest.raises(ArticleBuildRejectedError) as caught:
        complete_article(
            _observed(published_at=None),
            ExtractedContent(title=None, body=_EXTRACTED_BODY, published_at=None),
            DEFAULT_POLICY,
            source_id=7,
        )
    assert caught.value.rejection == QualityTooLow(
        defects=(AnalyzableArticleDefect.PUBLISHED_AT_MISSING,)
    )


def test_unclassified_validation_error_propagates() -> None:
    """構築時の未分類の検証エラーは元の例外のまま伝播する。"""
    original = ValidationError.from_exception_data(
        "AnalyzableArticle", [{"type": "missing", "loc": ("new_field",), "input": {}}]
    )
    with (
        patch.object(AnalyzableArticle, "build_or_reject", side_effect=original),
        pytest.raises(ValidationError) as caught,
    ):
        complete_article(
            _observed(),
            ExtractedContent(title=None, body=_EXTRACTED_BODY, published_at=None),
            DEFAULT_POLICY,
            source_id=7,
        )
    assert caught.value is original


def test_unexpected_build_exception_propagates() -> None:
    """構築の想定外例外は構築拒否に変換せず伝播する。"""
    original = RuntimeError("unexpected completion failure")
    with (
        patch.object(AnalyzableArticle, "build_or_reject", side_effect=original),
        pytest.raises(RuntimeError) as caught,
    ):
        complete_article(
            _observed(),
            ExtractedContent(title=None, body=_EXTRACTED_BODY, published_at=None),
            DEFAULT_POLICY,
            source_id=7,
        )
    assert caught.value is original
