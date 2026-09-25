"""補完Consumerの1回の処理で確定した成功・失敗だけを処理結果として記録する。"""

import asyncio

import httpx
import pytest

from local_tests.completion.support import (
    article_response,
    consumer_contract,
    decision_contract,
    delete_pending,
    seed_completed,
    seed_pending,
)
from tests.cloudwatch.records import metric_records


def completion_outcomes(output):
    return [
        record["result"]
        for record in metric_records(output, "processing_outcome")
        if record["stage"] == "completion"
    ]


@pytest.mark.asyncio
class TestDecidedResultIsRecordedOnce:
    """確定した成功・失敗は、Consumerの1回の処理につき1件だけ記録する。"""

    async def test_committed_completion_is_recorded_as_succeeded(
        self, system_database, completion_consumer, capsys
    ):
        """完成記事の保存をcommitできた処理はsucceededとして記録する。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-succeeded"
        )

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionSucceeded)
        assert completion_outcomes(capsys.readouterr().out) == ["succeeded"]

    async def test_rolled_back_completion_is_recorded_as_failed(
        self, system_database, completion_consumer, control_commit, capsys
    ):
        """完成記事のcommitに失敗した処理は成功とせず、failedとして記録する。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-rolled-back"
        )
        with control_commit(pending_article, phase="completed", fail=True) as fault:
            result = await completion_consumer.consume(pending_article.id)

        assert fault.error is not None
        assert isinstance(result, consumer_contract().CompletionFailed)
        assert completion_outcomes(capsys.readouterr().out) == ["failed"]

    async def test_retryable_failure_is_recorded_as_failed(
        self, system_database, completion_consumer, page_response, capsys
    ):
        """再試行する失敗も原因によらずfailedとして記録する。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-rate-limited"
        )
        page_response.return_value = httpx.Response(429)

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionFailed)
        assert isinstance(result.decision, decision_contract().RetryArticleCompletion)
        assert completion_outcomes(capsys.readouterr().out) == ["failed"]

    async def test_closed_failure_is_recorded_as_failed(
        self, system_database, completion_consumer, page_response, capsys
    ):
        """closedを確定した失敗はfailedとして記録する。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-forbidden"
        )
        page_response.return_value = httpx.Response(403)

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionFailed)
        assert isinstance(result.decision, decision_contract().CloseArticleCompletion)
        assert completion_outcomes(capsys.readouterr().out) == ["failed"]

    async def test_failed_close_commit_is_recorded_once_as_failed(
        self,
        system_database,
        completion_consumer,
        page_response,
        control_commit,
        capsys,
    ):
        """closedの確定に失敗して再試行へ変わっても、failedは1件だけ記録する。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-close-rollback"
        )
        page_response.return_value = httpx.Response(403)
        with control_commit(pending_article, phase="closed", fail=True) as fault:
            result = await completion_consumer.consume(pending_article.id)

        assert fault.error is not None
        assert isinstance(result, consumer_contract().CompletionFailed)
        assert isinstance(result.decision, decision_contract().RetryArticleCompletion)
        assert completion_outcomes(capsys.readouterr().out) == ["failed"]

    async def test_failure_audit_error_keeps_failed_record(
        self,
        system_database,
        completion_consumer,
        page_response,
        control_commit,
        capsys,
    ):
        """失敗監査を保存できなくても、failedの記録は残す。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-audit-failed"
        )
        page_response.return_value = httpx.Response(403)
        with control_commit(pending_article, phase="failure_audit", fail=True) as fault:
            result = await completion_consumer.consume(pending_article.id)

        assert fault.error is not None
        assert isinstance(result, consumer_contract().CompletionFailed)
        assert completion_outcomes(capsys.readouterr().out) == ["failed"]

    async def test_initial_database_failure_is_recorded_as_failed(
        self, system_database, first_read_failure_sessions, capsys
    ):
        """初回のDB照会に失敗した処理もfailedとして記録する。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-read-failed"
        )
        consumer = consumer_contract().ArticleCompletionConsumer(
            first_read_failure_sessions
        )

        result = await consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionFailed)
        assert completion_outcomes(capsys.readouterr().out) == ["failed"]


@pytest.mark.asyncio
class TestUndecidedResultIsNotRecorded:
    """処理不要と、別の処理に先を越された結果は記録しない。"""

    @pytest.fixture(
        params=[
            pytest.param(200, id="fetch_succeeds"),
            pytest.param(403, id="fetch_is_denied"),
        ]
    )
    def fetched_response(self, request):
        if request.param == 200:
            return article_response()
        return httpx.Response(request.param)

    async def test_missing_article_is_not_recorded(
        self, system_database, completion_consumer, capsys
    ):
        """未完成行が存在しないIDの処理は記録しない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-missing"
        )
        await delete_pending(system_database, pending_article)

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionNotRequired)
        assert result.reason == "missing"
        assert completion_outcomes(capsys.readouterr().out) == []

    async def test_closed_article_is_not_recorded(
        self, system_database, completion_consumer, capsys
    ):
        """closed済みの未完成記事の処理は記録しない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-closed", status="closed"
        )

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionNotRequired)
        assert result.reason == "closed"
        assert completion_outcomes(capsys.readouterr().out) == []

    async def test_existing_article_is_not_recorded(
        self, system_database, completion_consumer, capsys
    ):
        """同じURLの記事が完成済みで未完成行を削除しただけの処理は記録しない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-existing"
        )
        await seed_completed(system_database, pending_article)

        result = await completion_consumer.consume(pending_article.id)

        assert isinstance(result, consumer_contract().CompletionNotRequired)
        assert result.reason == "url_conflict"
        assert completion_outcomes(capsys.readouterr().out) == []

    async def test_article_finished_elsewhere_during_fetch_is_not_recorded(
        self,
        system_database,
        completion_consumer,
        gated_pages,
        fetched_response,
        capsys,
    ):
        """取得中に別の処理が未完成行を確定させた場合は、取得結果によらず記録しない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/outcome-superseded"
        )
        page = gated_pages(fetched_response)
        consuming = asyncio.create_task(completion_consumer.consume(pending_article.id))
        try:
            await page.wait_requested()
            await delete_pending(system_database, pending_article)
            page.release()
            result = await asyncio.wait_for(asyncio.shield(consuming), 15)
        finally:
            page.release()
            await asyncio.wait_for(
                asyncio.gather(consuming, return_exceptions=True), 15
            )

        assert isinstance(result, consumer_contract().CompletionNotRequired)
        assert result.reason == "superseded"
        assert completion_outcomes(capsys.readouterr().out) == []
