"""実DBとモックHTTPで、工程例外の原因が失敗の判断と記事の状態へ反映されることを確認する。"""

import httpx2
import pytest
from pydantic import Field, ValidationError

from app.collection.article_completion.errors import ArticleCompletionError
from app.collection.article_completion.html_extraction import (
    HtmlExtractionError,
    HtmlExtractionFailureReason,
)
from app.collection.domain.analyzable_article import AnalyzableArticle
from local_tests.completion.support import (
    consumer_contract,
    seed_pending,
    stored_completion,
)


@pytest.mark.asyncio
async def test_unclassified_validation_failure_keeps_article_open(
    system_database, completion_consumer, monkeypatch
):
    """未分類の検証失敗は元のValidationErrorを包んで再試行し、記事を終了しない。"""
    from app.collection.article_completion import article_completer

    class ArticleWithTitleConstraint(AnalyzableArticle):
        title: str = Field(pattern="^Allowed")

    monkeypatch.setattr(
        article_completer, "AnalyzableArticle", ArticleWithTitleConstraint
    )
    article = await seed_pending(system_database, "https://example.com/validation")

    result = await completion_consumer.consume(article.id)

    assert isinstance(result, consumer_contract().RetryCompletion)
    assert isinstance(result.error, ArticleCompletionError)
    assert isinstance(result.error.cause, ValidationError)
    assert result.error.__cause__ is result.error.cause
    assert result.error.cause.errors()[0]["type"] == "string_pattern_mismatch"
    assert result.code == "validation_error"
    assert result.requires_investigation
    stored = await stored_completion(system_database, article)
    assert stored.pending["status"] == "open"
    assert stored.articles == []


@pytest.mark.asyncio
async def test_html_content_type_failure_is_closed_with_reason(
    system_database, completion_consumer, page_response
):
    """HTML以外の応答は対応外のContent-Typeとして終了を確定する。"""
    article = await seed_pending(system_database, "https://example.com/pdf")
    page_response.return_value = httpx2.Response(
        200, headers={"Content-Type": "application/pdf"}, content=b"private-pdf-body"
    )

    result = await completion_consumer.consume(article.id)

    assert isinstance(result, consumer_contract().NoRetryCompletion)
    assert isinstance(result.cause, ArticleCompletionError)
    assert isinstance(result.cause.cause, HtmlExtractionError)
    assert result.code == "content_type_unsupported"
    stored = await stored_completion(system_database, article)
    assert stored.pending["status"] == "closed"


@pytest.mark.asyncio
async def test_empty_extraction_is_closed_with_reason(
    system_database, completion_consumer, monkeypatch
):
    """抽出結果がない場合は結果なしの理由で終了を確定する。"""
    from app.collection.article_completion import html_extraction

    article = await seed_pending(
        system_database, "https://example.com/empty-extraction"
    )
    monkeypatch.setattr(
        html_extraction.trafilatura, "bare_extraction", lambda *a, **k: None
    )

    result = await completion_consumer.consume(article.id)

    assert isinstance(result, consumer_contract().NoRetryCompletion)
    assert result.code == "extraction_empty"
    stored = await stored_completion(system_database, article)
    assert stored.pending["status"] == "closed"


@pytest.mark.asyncio
async def test_extractor_exception_is_retryable_with_cause(
    system_database, completion_consumer, monkeypatch
):
    """抽出器の例外は元の原因を保って再試行し、記事を終了しない。"""
    from app.collection.article_completion import html_extraction

    original = ValueError("private-parser-content")

    def fail_extraction(*args, **kwargs):
        raise original

    article = await seed_pending(
        system_database, "https://example.com/extractor-failure"
    )
    monkeypatch.setattr(html_extraction.trafilatura, "bare_extraction", fail_extraction)

    result = await completion_consumer.consume(article.id)

    assert isinstance(result, consumer_contract().RetryCompletion)
    assert isinstance(result.error, ArticleCompletionError)
    assert isinstance(result.error.cause, HtmlExtractionError)
    assert result.error.cause.reason is HtmlExtractionFailureReason.EXTRACTOR_RAISED
    assert result.error.cause.__cause__ is original
    assert result.code == "extraction_failed"
    stored = await stored_completion(system_database, article)
    assert stored.pending["status"] == "open"
