"""期限切れ整理の原子性と並行実行時の再確認を実DBで検証する。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.backfill.cleanup import (
    close_aged_out_completions,
    delete_aged_out_curations,
    exclude_aged_out_assessments,
    exclude_aged_out_embeddings,
)
from app.backfill.repository import PipelineBacklog
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.incomplete_article import IncompleteArticle
from tests.backfill.helpers import (
    complete_target,
    seed_target,
    target_exists,
)

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=7)
CASES = [
    ("curation", delete_aged_out_curations, "analyzable_article_ids_aged_out_curation"),
    ("assessment", exclude_aged_out_assessments, "curation_ids_aged_out_assessment"),
    (
        "embedding",
        exclude_aged_out_embeddings,
        "analyzed_article_ids_aged_out_embedding",
    ),
    (
        "completion",
        close_aged_out_completions,
        "incomplete_article_ids_aged_out_completion",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "cleanup", "selection"), CASES)
async def test_concurrent_cleanup_commits_only_once(
    db_session,
    session_factory,
    sample_source,
    sample_categories,
    monkeypatch,
    stage,
    cleanup,
    selection,
):
    """同じ候補を抽出した二実行でも整理を一度だけ確定する。"""
    await seed_target(
        db_session,
        sample_source,
        sample_categories[0],
        stage,
        CUTOFF - timedelta(days=1),
    )
    selected = asyncio.Barrier(2)
    original = getattr(PipelineBacklog, selection)

    async def select_together(self, **kwargs):
        ids = await original(self, **kwargs)
        await selected.wait()
        return ids

    monkeypatch.setattr(PipelineBacklog, selection, select_together)
    result = await asyncio.wait_for(
        asyncio.gather(
            cleanup(session_factory, created_before=CUTOFF),
            cleanup(session_factory, created_before=CUTOFF),
        ),
        timeout=10,
    )
    assert sorted(result) == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "cleanup", "selection"), CASES)
async def test_completion_between_selection_and_lock_is_preserved(
    db_session,
    session_factory,
    sample_source,
    sample_categories,
    monkeypatch,
    stage,
    cleanup,
    selection,
):
    """抽出後に完了した記事はロック取得後の再照会で整理から外す。"""
    target = await seed_target(
        db_session,
        sample_source,
        sample_categories[0],
        stage,
        CUTOFF - timedelta(days=1),
    )
    selected, release = asyncio.Event(), asyncio.Event()
    original = getattr(PipelineBacklog, selection)

    async def pause_after_selection(self, **kwargs):
        ids = await original(self, **kwargs)
        selected.set()
        await release.wait()
        return ids

    monkeypatch.setattr(PipelineBacklog, selection, pause_after_selection)
    task = asyncio.create_task(cleanup(session_factory, created_before=CUTOFF))
    try:
        await asyncio.wait_for(selected.wait(), 5)
        await complete_target(db_session, target, stage, sample_categories[0])
        await db_session.commit()
        release.set()
        assert await asyncio.wait_for(task, 5) == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await target_exists(db_session, target, stage)


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "cleanup", "selection"), CASES)
async def test_cleanup_waits_for_uncommitted_completion(
    db_session,
    session_factory,
    sample_source,
    sample_categories,
    monkeypatch,
    stage,
    cleanup,
    selection,
):
    """未commitの完了保存と競合しても確定後の結果を整理しない。"""
    target = await seed_target(
        db_session,
        sample_source,
        sample_categories[0],
        stage,
        CUTOFF - timedelta(days=1),
    )
    await complete_target(db_session, target, stage, sample_categories[0])
    reached = asyncio.Event()
    method = f"lock_aged_out_{stage}"
    original = getattr(PipelineBacklog, method)

    async def notify_lock(self, *args, **kwargs):
        reached.set()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(PipelineBacklog, method, notify_lock)
    task = asyncio.create_task(cleanup(session_factory, created_before=CUTOFF))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        await db_session.commit()
        assert await asyncio.wait_for(task, 5) == 0
    finally:
        await db_session.rollback()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await target_exists(db_session, target, stage)


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "cleanup", "selection"), CASES)
async def test_expired_selection_rechecks_disappeared_target(
    db_session,
    session_factory,
    sample_source,
    sample_categories,
    monkeypatch,
    stage,
    cleanup,
    selection,
):
    """抽出後に対象が削除されても整理件数を増やさない。"""
    from sqlalchemy import delete

    target = await seed_target(
        db_session,
        sample_source,
        sample_categories[0],
        stage,
        CUTOFF - timedelta(days=1),
    )
    original = getattr(PipelineBacklog, selection)

    async def remove_after_selection(self, **kwargs):
        ids = await original(self, **kwargs)
        if stage == "completion":
            await db_session.execute(
                delete(IncompleteArticle).where(
                    IncompleteArticle.id == target.target_id
                )
            )
        else:
            await db_session.execute(
                delete(AnalyzableArticleRecord).where(
                    AnalyzableArticleRecord.id == target.article_id
                )
            )
        await db_session.commit()
        return ids

    monkeypatch.setattr(PipelineBacklog, selection, remove_after_selection)
    assert await cleanup(session_factory, created_before=CUTOFF) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "cleanup", "limit"),
    [
        ("curation", delete_aged_out_curations, 200),
        ("assessment", exclude_aged_out_assessments, 50),
        ("embedding", exclude_aged_out_embeddings, 50),
        ("completion", close_aged_out_completions, 50),
    ],
)
async def test_cleanup_obeys_per_run_limit(
    db_session, session_factory, sample_source, sample_categories, stage, cleanup, limit
):
    """期限切れ整理も工程ごとの一回上限に収める。"""
    for i in range(limit + 1):
        await seed_target(
            db_session,
            sample_source,
            sample_categories[0],
            stage,
            CUTOFF - timedelta(hours=1, minutes=i),
        )
    assert await cleanup(session_factory, created_before=CUTOFF) == limit
    assert await cleanup(session_factory, created_before=CUTOFF) == 1


@pytest.mark.asyncio
async def test_completion_aged_out_closes_row(
    db_session, session_factory, sample_source, sample_categories
):
    """期限切れの未完成行は削除せずclosedにし、二度目の整理では対象にしない。"""
    target = await seed_target(
        db_session,
        sample_source,
        sample_categories[0],
        "completion",
        CUTOFF - timedelta(days=1),
    )
    assert await close_aged_out_completions(session_factory, created_before=CUTOFF) == 1
    incomplete = await db_session.get(IncompleteArticle, target.target_id)
    assert incomplete.status == "closed"
    assert incomplete.leased_until is None
    assert await close_aged_out_completions(session_factory, created_before=CUTOFF) == 0
