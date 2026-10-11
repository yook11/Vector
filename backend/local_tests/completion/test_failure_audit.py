"""実DBとモックHTTPで、工程例外の原因が失敗監査と記事の状態へ反映されることを確認する。"""

import json

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
async def test_http_failure_audit_keeps_response_status(
    system_database, completion_consumer, page_response
):
    """工程例外で包んだHTTP失敗でも、監査に元のステータスと例外チェーンを残す。"""
    article = await seed_pending(system_database, "https://example.com/http-audit")
    page_response.return_value = httpx2.Response(429, headers={"Retry-After": "120"})

    await completion_consumer.consume(article.id)

    stored = await stored_completion(system_database, article)
    assert len(stored.audits) == 1
    audit = stored.audits[0]
    assert audit["outcome_code"] == "http_response_error"
    assert audit["payload"]["http_status"] == 429
    assert audit["payload"]["error_chain"][:2] == [
        "app.collection.article_completion.errors.ArticleCompletionError",
        "app.http.errors.HttpResponseError",
    ]


@pytest.mark.asyncio
async def test_transport_failure_audit_keeps_reason(
    system_database, completion_consumer, page_response
):
    """工程例外で包んだ通信失敗でも、監査に元の通信失敗理由を残す。"""
    article = await seed_pending(system_database, "https://example.com/transport-audit")
    page_response.side_effect = httpx2.ReadTimeout("test timeout")

    await completion_consumer.consume(article.id)

    stored = await stored_completion(system_database, article)
    assert len(stored.audits) == 1
    audit = stored.audits[0]
    assert audit["outcome_code"] == "http_transport_error"
    assert audit["payload"]["reason_code"] == "timeout"
    assert audit["payload"]["error_chain"][:2] == [
        "app.collection.article_completion.errors.ArticleCompletionError",
        "app.http.errors.HttpTransportError",
    ]


@pytest.mark.asyncio
async def test_build_rejection_audit_keeps_domain_defects(
    system_database, completion_consumer
):
    """既知のタイトル長違反による構築拒否の具体的な理由を監査に残す。"""
    article = await seed_pending(
        system_database, "https://example.com/rejection-audit", title="T" * 501
    )

    await completion_consumer.consume(article.id)

    stored = await stored_completion(system_database, article)
    assert len(stored.audits) == 1
    audit = stored.audits[0]
    assert audit["event_type"] == "rejected"
    assert audit["outcome_code"] == "article_invalid"
    assert audit["payload"]["defects"] == ["analyzable_article_title_too_long"]
    assert audit["payload"]["failure_kind"] == "article_build"
    assert audit["payload"]["error_chain"][:2] == [
        "app.collection.article_completion.errors.ArticleCompletionError",
        "app.collection.article_completion.errors.ArticleBuildRejectedError",
    ]


@pytest.mark.asyncio
async def test_unclassified_validation_failure_keeps_article_open(
    system_database, completion_consumer, monkeypatch
):
    """未分類の検証失敗は元のValidationErrorを包んで再試行し、記事を終了しない。"""
    from app.collection.article_completion import html_completion

    class ArticleWithTitleConstraint(AnalyzableArticle):
        title: str = Field(pattern="^Allowed")

    monkeypatch.setattr(
        html_completion, "AnalyzableArticle", ArticleWithTitleConstraint
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
    assert len(stored.audits) == 1
    assert stored.audits[0]["event_type"] == "failed"
    assert stored.audits[0]["outcome_code"] == "validation_error"


@pytest.mark.asyncio
async def test_html_content_type_failure_is_closed_with_audited_reason(
    system_database, completion_consumer, page_response
):
    """HTML以外の応答は終了を確定し、監査に抽出段階・理由・Content-Typeを残す。"""
    article = await seed_pending(system_database, "https://example.com/pdf")
    page_response.return_value = httpx2.Response(
        200, headers={"Content-Type": "application/pdf"}, content=b"private-pdf-body"
    )

    result = await completion_consumer.consume(article.id)

    assert isinstance(result, consumer_contract().NoRetryCompletion)
    assert isinstance(result.cause, ArticleCompletionError)
    assert isinstance(result.cause.cause, HtmlExtractionError)
    stored = await stored_completion(system_database, article)
    assert stored.pending["status"] == "closed"
    assert len(stored.audits) == 1
    audit = stored.audits[0]
    assert audit["event_type"] == "rejected"
    assert audit["outcome_code"] == "content_type_unsupported"
    assert audit["payload"]["failure_kind"] == "html_extraction"
    assert audit["payload"]["reason_code"] == "content_type_unsupported"
    assert audit["payload"]["content_type"] == "application/pdf"
    assert audit["payload"]["error_chain"] == [
        "app.collection.article_completion.errors.ArticleCompletionError",
        "app.collection.article_completion.html_extraction.HtmlExtractionError",
    ]
    assert "private-pdf-body" not in json.dumps(audit)


@pytest.mark.asyncio
async def test_empty_extraction_is_closed_with_audited_reason(
    system_database, completion_consumer, monkeypatch
):
    """抽出結果がない場合は終了を確定し、監査に結果なしの理由を残す。"""
    from app.collection.article_completion import html_extraction

    article = await seed_pending(
        system_database, "https://example.com/empty-extraction"
    )
    monkeypatch.setattr(
        html_extraction.trafilatura, "bare_extraction", lambda *a, **k: None
    )

    result = await completion_consumer.consume(article.id)

    assert isinstance(result, consumer_contract().NoRetryCompletion)
    stored = await stored_completion(system_database, article)
    assert stored.pending["status"] == "closed"
    assert len(stored.audits) == 1
    assert stored.audits[0]["outcome_code"] == "extraction_empty"
    assert stored.audits[0]["payload"]["reason_code"] == "extraction_empty"


@pytest.mark.asyncio
async def test_extractor_exception_is_retryable_with_audited_cause_chain(
    system_database, completion_consumer, monkeypatch
):
    """抽出器の例外は元の原因を保って再試行し、監査には自由文を除いた原因連鎖を残す。"""
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
    stored = await stored_completion(system_database, article)
    assert stored.pending["status"] == "open"
    assert len(stored.audits) == 1
    audit = stored.audits[0]
    assert audit["event_type"] == "failed"
    assert audit["outcome_code"] == "extraction_failed"
    assert audit["payload"]["failure_kind"] == "html_extraction"
    assert audit["payload"]["reason_code"] == "extraction_failed"
    assert audit["payload"]["error_chain"] == [
        "app.collection.article_completion.errors.ArticleCompletionError",
        "app.collection.article_completion.html_extraction.HtmlExtractionError",
        "builtins.ValueError",
    ]
    assert "private-parser-content" not in json.dumps(audit)
