"""記事取得待ちの間に補完ConsumerがDBトランザクションを保持しないことを確認する。"""

import asyncio

import pytest

from local_tests.completion.support import (
    article_response,
    consumer_contract,
    has_active_collection_transaction,
    seed_pending,
    stored_completion,
)


@pytest.mark.asyncio
class TestHttpWaitReleasesDatabaseResources:
    """HTTP応答待ちでは補完用DB接続とトランザクションを保持しない。"""

    async def test_http_wait_does_not_keep_database_transaction(
        self, system_database, completion_consumer, completion_engine, gated_pages
    ):
        """記事のHTTP応答を止めた間、補完用DB接続が返却されトランザクションも終了している。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/session-boundary"
        )
        contract = consumer_contract()
        page = gated_pages(article_response())
        task = asyncio.create_task(completion_consumer.consume(pending_article.id))
        try:
            await page.wait_requested()
            assert completion_engine.pool.checkedout() == 0
            assert not await has_active_collection_transaction(system_database)
        finally:
            page.release()
            result = await asyncio.wait_for(task, 15)
        assert isinstance(result, contract.CompletionSucceeded)

    async def test_external_cancellation_releases_resources_without_failure_result(
        self,
        system_database,
        completion_consumer,
        completion_engine,
        gated_pages,
        http_clients,
    ):
        """HTTP待機中の外部キャンセルを伝播し、接続を解放して未完成行と監査を変えない。"""
        pending_article = await seed_pending(
            system_database, "https://example.com/cancelled"
        )
        before = await stored_completion(system_database, pending_article)
        page = gated_pages(article_response())
        task = asyncio.create_task(completion_consumer.consume(pending_article.id))
        try:
            await page.wait_requested()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            page.release()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)

        assert http_clients and all(client.is_closed for client in http_clients)
        assert completion_engine.pool.checkedout() == 0
        assert not await has_active_collection_transaction(system_database)
        assert await stored_completion(system_database, pending_article) == before
