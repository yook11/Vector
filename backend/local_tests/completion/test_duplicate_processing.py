"""重複受信と並行処理が先に確定した補完結果を変えないことを確認する。"""

from functools import partial

import httpx
import pytest

from local_tests.completion import concurrent_processing
from local_tests.completion.support import (
    article_response,
    consumer_contract,
    seed_pending,
    stored_completion,
)


@pytest.mark.asyncio
class TestFirstCommittedCompletionIsPreserved:
    """重複して補完しても、先に確定した記事またはclosed状態が維持される。"""

    @pytest.fixture
    def later_consumer(self, completion_sessions):
        return consumer_contract().ArticleCompletionConsumer(completion_sessions)

    @pytest.fixture
    def run_completion_race(
        self,
        system_database,
        completion_consumer,
        later_consumer,
        gated_pages,
        control_commit,
    ):
        return partial(
            concurrent_processing.run_completion_race,
            system_database=system_database,
            first_consumer=completion_consumer,
            second_consumer=later_consumer,
            gated_pages=gated_pages,
            control_commit=control_commit,
        )

    @pytest.fixture(
        params=[
            pytest.param(200, id="later_fetch_succeeds"),
            pytest.param(403, id="later_fetch_is_denied"),
        ]
    )
    def later_response(self, request):
        if request.param == 200:
            return article_response("Later content")
        return httpx.Response(request.param)

    async def test_redelivery_keeps_first_article_audit_and_outbox(
        self,
        completion_consumer,
        system_database,
        page_response,
        http_boundary,
    ):
        """完成後の再受信はHTTPを追加せず、初回の記事・監査・Outboxを保持する。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/redelivery"
        )
        initial_result = await completion_consumer.consume(pending_article.id)
        assert isinstance(initial_result, consumer_contract().CompletionSucceeded)
        stored_before_redelivery = await stored_completion(
            system_database, pending_article
        )
        requests_before_redelivery = len(http_boundary.requests)
        page_response.return_value = article_response("Later content must not be saved")

        redelivery_result = await completion_consumer.consume(pending_article.id)

        assert isinstance(redelivery_result, consumer_contract().CompletionNotRequired)
        assert redelivery_result.reason == "missing"
        assert len(http_boundary.requests) == requests_before_redelivery
        assert (
            await stored_completion(system_database, pending_article)
            == stored_before_redelivery
        )

    async def test_later_completion_keeps_first_completed_article(
        self,
        system_database,
        run_completion_race,
        later_response,
    ):
        """先に完成した記事を後続処理が変更せず、成功監査とOutboxも増やさない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/completed-first"
        )
        contract = consumer_contract()

        first_result, later_result = await run_completion_race(
            pending_article,
            first_response=article_response("First content"),
            second_response=later_response,
            first_commit_phase="completed",
        )

        assert isinstance(first_result, contract.CompletionSucceeded)
        assert isinstance(later_result, contract.CompletionNotRequired)
        assert later_result.reason == "superseded"
        stored_after_competition = await stored_completion(
            system_database, pending_article
        )
        assert stored_after_competition.pending is None
        assert len(stored_after_competition.articles) == 1
        assert (
            "First content" in stored_after_competition.articles[0]["original_content"]
        )
        assert (
            "Later content"
            not in stored_after_competition.articles[0]["original_content"]
        )
        assert len(stored_after_competition.successes) == 1
        assert len(stored_after_competition.outbox) == 1

    async def test_later_completion_keeps_first_closed_state(
        self,
        system_database,
        run_completion_race,
        later_response,
    ):
        """先にclosedになった記事を後続処理が完成させず、失敗監査も増やさない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/closed-first"
        )
        contract = consumer_contract()

        first_result, later_result = await run_completion_race(
            pending_article,
            first_response=httpx.Response(403),
            second_response=later_response,
            first_commit_phase="closed",
        )

        assert isinstance(first_result, contract.CompletionFailed)
        assert isinstance(later_result, contract.CompletionNotRequired)
        assert later_result.reason == "superseded"
        stored_after_competition = await stored_completion(
            system_database, pending_article
        )
        assert stored_after_competition.pending is not None
        assert stored_after_competition.pending["status"] == "closed"
        assert stored_after_competition.articles == []
        assert stored_after_competition.successes == []
        assert stored_after_competition.outbox == []
        assert len(stored_after_competition.audits) == 1
