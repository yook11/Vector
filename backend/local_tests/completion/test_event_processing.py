"""未完成記事のDB状態から、実Consumerによる補完結果の確定を確認する。"""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from app.db.errors import DatabaseError
from app.http.errors import HttpResponseError
from local_tests.completion.support import (
    PUBLISHED_AT,
    article_response,
    consumer_contract,
    decision_contract,
    delete_pending,
    seed_completed,
    seed_pending,
    stored_completion,
)


@pytest.mark.asyncio
class TestArticlesNotRequiringCompletion:
    """補完不要な記事は状態に応じて終了し、既存の完成結果を変更しない。"""

    async def test_missing_article_finishes_without_http(
        self, system_database, completion_consumer, http_boundary
    ):
        """未完成行が存在しないIDは処理不要となり、HTTPも保存も実行しない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/skip", status="closed"
        )
        await delete_pending(system_database, pending_article)
        stored_before_processing = await stored_completion(
            system_database, pending_article
        )

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionNotRequired)
        assert result.reason == "missing"
        assert http_boundary.requests == []
        assert (
            await stored_completion(system_database, pending_article)
            == stored_before_processing
        )

    async def test_closed_article_finishes_without_http(
        self, system_database, completion_consumer, http_boundary
    ):
        """closedの未完成記事は処理不要となり、HTTPも保存も実行しない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/skip", status="closed"
        )
        stored_before_processing = await stored_completion(
            system_database, pending_article
        )

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionNotRequired)
        assert result.reason == "closed"
        assert http_boundary.requests == []
        assert (
            await stored_completion(system_database, pending_article)
            == stored_before_processing
        )

    async def test_existing_article_is_kept_and_pending_row_is_removed(
        self, system_database, completion_consumer, page_response
    ):
        """同じURLの記事が別経路で完成済みなら、既存記事を保ち未完成行だけを削除する。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/existing"
        )
        await seed_completed(system_database, pending_article)
        stored_before_processing = await stored_completion(
            system_database, pending_article
        )
        page_response.return_value = article_response("Content that must not overwrite")

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionNotRequired)
        assert result.reason == "url_conflict"
        stored_after_processing = await stored_completion(
            system_database, pending_article
        )
        assert stored_after_processing.pending is None
        assert stored_after_processing.articles == stored_before_processing.articles
        assert stored_after_processing.audits == stored_before_processing.audits
        assert stored_after_processing.outbox == stored_before_processing.outbox


@pytest.mark.asyncio
class TestCompletionIsCommittedAtomically:
    """完成記事・未完成行削除・成功監査・Outboxをまとめて確定または取り消す。"""

    @pytest.mark.parametrize("state", ["open", "running"])
    async def test_eligible_article_commits_completion_and_event(
        self, system_database, completion_consumer, http_boundary, state
    ):
        """非closedの対象だけを完成させ、未完成行削除・成功監査・Outboxを確定する。"""
        unrelated_article = await seed_pending(
            system_database, "https://example.com/other"
        )
        unrelated_before_processing = await stored_completion(
            system_database, unrelated_article
        )
        pending_article = await seed_pending(
            system_database, "https://example.com/target", status=state
        )

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionSucceeded)
        stored = await stored_completion(system_database, pending_article)
        assert stored.pending is None
        assert len(stored.articles) == 1
        article = stored.articles[0]
        assert article["id"] == result.analyzable_article_id
        assert article["source_id"] == pending_article.source_id
        assert article["original_title"] == pending_article.title
        assert "Target discovery" in article["original_content"]
        assert article["published_at"] == PUBLISHED_AT
        assert len(stored.successes) == 1
        assert stored.successes[0]["article_id"] == article["id"]
        assert len(stored.outbox) == 1
        assert stored.outbox[0]["schema_version"] == 1
        assert stored.outbox[0]["payload"] == {"analyzable_article_id": article["id"]}
        assert {
            str(request.url)
            for request in http_boundary.requests
            if request.url.path != "/robots.txt"
        } == {pending_article.url}
        assert (
            await stored_completion(system_database, unrelated_article)
            == unrelated_before_processing
        )

    async def test_database_failure_rolls_back_all_completion_writes(
        self, system_database, completion_consumer, control_commit
    ):
        """4操作の実行後にDB障害を起こして全てを戻し、同じ記事を再処理できる。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/rollback"
        )
        with control_commit(pending_article, phase="completed", fail=True) as fault:
            failed_result = await completion_consumer.consume(pending_article.id)

        assert fault.error is not None
        assert fault.state == {"status": None, "articles": 1, "audits": 1, "outbox": 1}
        assert isinstance(failed_result, consumer_contract().CompletionFailed)
        assert isinstance(failed_result.error, DatabaseError)
        assert failed_result.error.__cause__ is fault.error
        assert isinstance(
            failed_result.decision, decision_contract().RetryArticleCompletion
        )
        stored_after_failure = await stored_completion(system_database, pending_article)
        assert stored_after_failure.pending is not None
        assert stored_after_failure.pending["status"] != "closed"
        assert stored_after_failure.articles == []
        assert stored_after_failure.successes == []
        assert stored_after_failure.outbox == []

        retry_result = await completion_consumer.consume(pending_article.id)
        assert isinstance(retry_result, consumer_contract().CompletionSucceeded)
        stored_after_retry = await stored_completion(system_database, pending_article)
        assert stored_after_retry.pending is None
        assert len(stored_after_retry.articles) == 1
        assert len(stored_after_retry.successes) == 1
        assert len(stored_after_retry.outbox) == 1


@pytest.mark.asyncio
class TestCompletionFailurePreservesDecisionAndState:
    """補完失敗の判断とDB状態を対応させ、監査の二次障害でも元の判断を維持する。"""

    async def test_retryable_http_failure_returns_error_and_wait_without_closing(
        self, system_database, completion_consumer, page_response
    ):
        """429では未完成行を残し、元のHTTPエラーと受信時刻基準の再試行判断を返す。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/rate-limited"
        )
        page_response.return_value = httpx.Response(429, headers={"Retry-After": "120"})

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionFailed)
        assert isinstance(result.error, HttpResponseError)
        assert result.error.status_code == 429
        assert isinstance(result.error.__cause__, httpx.HTTPStatusError)
        assert isinstance(result.decision, decision_contract().RetryArticleCompletion)
        assert result.decision.retry_at is not None
        assert result.decision.retry_at.value == result.error.received_at + timedelta(
            seconds=120
        )
        stored = await stored_completion(system_database, pending_article)
        assert stored.pending is not None
        assert stored.pending["status"] != "closed"
        assert stored.articles == []
        assert stored.successes == []
        assert stored.outbox == []

    async def test_permanent_failure_commits_closed_before_returning(
        self, system_database, completion_consumer, page_response
    ):
        """403では終了判断を返す前にclosedを確定し、完成記事や成功イベントを作らない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/forbidden"
        )
        page_response.return_value = httpx.Response(403)

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionFailed)
        assert isinstance(result.error, HttpResponseError)
        assert result.error.status_code == 403
        assert isinstance(result.decision, decision_contract().CloseArticleCompletion)
        stored = await stored_completion(system_database, pending_article)
        assert stored.pending is not None
        assert stored.pending["status"] == "closed"
        assert stored.articles == []
        assert stored.successes == []
        assert stored.outbox == []

    async def test_failed_close_commit_returns_retry_and_leaves_row_open(
        self, system_database, completion_consumer, page_response, control_commit
    ):
        """終了対象のHTTP失敗でもclosedの確定に失敗したら、終了結果を返さない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/close-rollback"
        )
        page_response.return_value = httpx.Response(403)
        with control_commit(pending_article, phase="closed", fail=True) as fault:
            result = await completion_consumer.consume(pending_article.id)

        assert fault.error is not None
        assert isinstance(result, consumer_contract().CompletionFailed)
        assert isinstance(result.error, DatabaseError)
        assert isinstance(result.decision, decision_contract().RetryArticleCompletion)
        stored = await stored_completion(system_database, pending_article)
        assert stored.pending is not None
        assert stored.pending["status"] != "closed"
        assert stored.articles == []
        assert stored.successes == []
        assert stored.outbox == []

    async def test_failure_audit_error_keeps_retry_decision_and_wait(
        self, system_database, completion_consumer, page_response, control_commit
    ):
        """失敗監査を保存できなくても元のHTTP失敗と再試行時刻を維持し、closedにしない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/audit-retry"
        )
        page_response.return_value = httpx.Response(429, headers={"Retry-After": "120"})
        with control_commit(pending_article, phase="failure_audit", fail=True) as fault:
            result = await completion_consumer.consume(pending_article.id)

        assert fault.error is not None
        assert isinstance(result, consumer_contract().CompletionFailed)
        assert isinstance(result.error, HttpResponseError)
        assert result.error.status_code == 429
        assert isinstance(result.decision, decision_contract().RetryArticleCompletion)
        assert result.decision.retry_at is not None
        assert result.decision.retry_at.value == result.error.received_at + timedelta(
            seconds=120
        )
        stored_after_failure = await stored_completion(system_database, pending_article)
        assert stored_after_failure.pending is not None
        assert stored_after_failure.pending["status"] != "closed"
        assert stored_after_failure.articles == []
        assert stored_after_failure.audits == []
        assert stored_after_failure.outbox == []

    async def test_failure_audit_error_keeps_committed_closed_state(
        self, system_database, completion_consumer, page_response, control_commit
    ):
        """失敗監査を保存できなくても元のHTTP失敗と終了判断・確定済みclosedを維持する。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/audit-closed"
        )
        page_response.return_value = httpx.Response(403, headers={"Retry-After": "120"})
        with control_commit(pending_article, phase="failure_audit", fail=True) as fault:
            result = await completion_consumer.consume(pending_article.id)

        assert fault.error is not None
        assert isinstance(result, consumer_contract().CompletionFailed)
        assert isinstance(result.error, HttpResponseError)
        assert result.error.status_code == 403
        assert isinstance(result.decision, decision_contract().CloseArticleCompletion)
        stored_after_failure = await stored_completion(system_database, pending_article)
        assert stored_after_failure.pending is not None
        assert stored_after_failure.pending["status"] == "closed"
        assert stored_after_failure.articles == []
        assert stored_after_failure.audits == []
        assert stored_after_failure.outbox == []

    async def test_initial_database_failure_does_not_become_missing_or_closed(
        self, system_database, first_read_failure_sessions, http_boundary
    ):
        """初回DB照会が失敗したら対象なしと見なさず、原因を持つ再試行結果を返す。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/read-failed"
        )
        before = await stored_completion(system_database, pending_article)
        consumer = consumer_contract().ArticleCompletionConsumer(
            first_read_failure_sessions
        )

        result = await consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionFailed)
        assert isinstance(result.error, DatabaseError)
        assert isinstance(result.error.__cause__, DBAPIError)
        assert isinstance(result.decision, decision_contract().RetryArticleCompletion)
        assert result.decision.code == "unknown"
        assert result.decision.requires_investigation
        assert http_boundary.requests == []
        after = await stored_completion(system_database, pending_article)
        assert after.pending == before.pending
        assert after.articles == after.successes == after.outbox == []
