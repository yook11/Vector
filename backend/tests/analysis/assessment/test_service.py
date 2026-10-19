"""AssessmentServiceの正常終了・原因保持・保存とOutboxの原子性を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.domain.result import (
    InScope,
    InScopeCategory,
    OutOfScope,
)
from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.assessment.repository import CategoryEnumDatabaseMismatchError
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
    AssessmentService,
)
from app.analysis.logging import create_article_analysis_logger
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import (
    AnalyzedArticleRecord as AnalyzedArticleRecordORM,
)
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource
from app.models.out_of_scope_article_record import (
    OutOfScopeArticleRecord as OutOfScopeArticleRecordORM,
)
from app.models.outbox_event import OutboxEvent
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result
from tests.outbox import RejectOutboxInsert

_AI_MODEL = "gemini-2.5-flash-lite"
_PROCESSING_OUTCOME_METRIC = "vector.assessment.processing_outcome"


@pytest.fixture
def assessment_logger():
    return create_article_analysis_logger().bind(stage="assessment")


async def _make_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/a",
) -> AnalyzableArticleRecord:
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="Original",
        original_content="c" * 100,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


async def _make_extraction(
    db_session: AsyncSession, article: AnalyzableArticleRecord
) -> ArticleCuration:
    extraction = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title="title",
        summary="summary text",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


def _ready(extraction: ArticleCuration) -> ReadyForAssessment:
    return ReadyForAssessment(
        curation_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=extraction.summary,
    )


def _in_scope_call(
    category: InScopeCategory = InScopeCategory.AI,
) -> AssessmentCall[InScope]:
    return AssessmentCall(
        result=InScope(
            category=category,
            investor_take="bullish",
        ),
        raw_response='{"category":"ai"}',
        raw_category=category.value,
        prompt_version="testver1",
        model_name=_AI_MODEL,
    )


def _out_of_scope_call() -> AssessmentCall[OutOfScope]:
    return AssessmentCall(
        result=OutOfScope(investor_take="not relevant"),
        raw_response='{"category":"out_of_scope"}',
        raw_category="out_of_scope",
        prompt_version="testver1",
        model_name=_AI_MODEL,
    )


def _make_assessor(
    *,
    return_envelope: AssessmentCall[InScope] | AssessmentCall[OutOfScope] | None = None,
    side_effect: object = None,
) -> BaseAssessor:
    mock = MagicMock(spec=BaseAssessor)
    if side_effect is not None:
        mock.assess = AsyncMock(side_effect=side_effect)
    else:
        mock.assess = AsyncMock(return_value=return_envelope or _in_scope_call())
    return mock


@pytest.mark.asyncio
async def test_in_scope_success_passes_snapshot_to_repository(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    assessment_logger,
) -> None:
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    call = _in_scope_call(category=InScopeCategory.AI)
    assessor = _make_assessor(return_envelope=call)
    save_mock = AsyncMock(return_value=777)

    svc = AssessmentService(session_factory)
    with patch(
        "app.analysis.assessment.repository.AssessmentRepository.save_in_scope",
        new=save_mock,
    ):
        result = await svc.execute(
            _ready(extraction),
            assessor,
            analyzable_article_id=article.id,
            logger=assessment_logger,
        )

    assert result == AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, 777)
    saved_article = save_mock.await_args.args[0]
    assert isinstance(saved_article, InScopeAnalyzedArticle)
    assert saved_article.curation_id == extraction.id
    assert saved_article.title == extraction.translated_title
    assert saved_article.summary == extraction.summary
    assert saved_article.assessment_result == call.result


@pytest.mark.asyncio
async def test_out_of_scope_success_persists_snapshot(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    assessment_logger,
) -> None:
    """out-of-scope 成功で、Stage 3 由来の snapshot を保存する。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_out_of_scope_call())

    svc = AssessmentService(session_factory)
    result = await svc.execute(
        _ready(extraction),
        assessor,
        analyzable_article_id=article.id,
        logger=assessment_logger,
    )
    assert result == AssessmentCompletion(AssessmentCompletionKind.OUT_OF_SCOPE)

    # 本体 DB (out_of_scope_articles) に Stage 3 由来 snapshot が永続化されている
    persisted = (
        await db_session.execute(
            select(OutOfScopeArticleRecordORM).where(
                OutOfScopeArticleRecordORM.curation_id == extraction.id
            )
        )
    ).scalar_one()
    assert persisted.translated_title == extraction.translated_title
    assert persisted.summary == extraction.summary


@pytest.mark.asyncio
async def test_race_lost_does_not_add_outbox_event(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    assessment_logger,
) -> None:
    """実DBの重複スキップは処理済みとなり、Outboxを追加しない。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    # 勝者 row を先に焼いておく (本 test は敗者経路)
    winner = AnalyzedArticleRecordORM(
        curation_id=extraction.id,
        translated_title="title",
        summary="summary text",
        category_id=sample_categories[0].id,
        investor_take="bullish",
    )
    db_session.add(winner)
    await db_session.commit()

    assessor = _make_assessor(
        return_envelope=_in_scope_call(category=InScopeCategory.AI)
    )

    svc = AssessmentService(session_factory)
    result = await svc.execute(
        _ready(extraction),
        assessor,
        analyzable_article_id=article.id,
        logger=assessment_logger,
    )

    assert result == AssessmentCompletion(AssessmentCompletionKind.ALREADY_ASSESSED)

    async with session_factory() as reader:
        outbox_events = (
            (await reader.execute(select(OutboxEvent.event_id))).scalars().all()
        )
    assert outbox_events == []


@pytest.mark.asyncio
async def test_unknown_category_raises_enum_db_mismatch(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    assessment_logger,
) -> None:
    """``in_scope.category.value`` が catalog 未登録 →
    ``CategoryEnumDatabaseMismatchError`` raise。

    ``sample_categories`` fixture を使わない (catalog 未登録状態を作る) ため、
    Repository.save 内部の ``_get_category_id_by_slug`` が必ず None を返し、
    Repository が ``CategoryEnumDatabaseMismatchError`` を raise する。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(
        return_envelope=_in_scope_call(category=InScopeCategory.AI)
    )

    svc = AssessmentService(session_factory)
    with pytest.raises(CategoryEnumDatabaseMismatchError) as excinfo:
        await svc.execute(
            _ready(extraction),
            assessor,
            analyzable_article_id=article.id,
            logger=assessment_logger,
        )
    assert excinfo.value.missing == {"ai"}


@pytest.mark.asyncio
async def test_result_rolled_back_when_commit_fails(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    assessment_logger,
) -> None:
    """``session.commit`` が raise すると業務 INSERT は残らない。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(
        return_envelope=_in_scope_call(category=InScopeCategory.AI)
    )

    svc = AssessmentService(session_factory)
    # AsyncSession.commit() を patch して、保存の後に raise
    boom = RuntimeError("commit failed")
    with patch(
        "sqlalchemy.ext.asyncio.AsyncSession.commit",
        new=AsyncMock(side_effect=boom),
    ):
        with pytest.raises(RuntimeError, match="commit failed"):
            await svc.execute(
                _ready(extraction),
                assessor,
                analyzable_article_id=article.id,
                logger=assessment_logger,
            )

    rows = (
        (
            await db_session.execute(
                select(AnalyzedArticleRecordORM).where(
                    AnalyzedArticleRecordORM.curation_id == extraction.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_out_of_scope_race_lost_keeps_winner_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    assessment_logger,
) -> None:
    """out-of-scope 経路の race lost では、先に保存した行を変えない。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    # 勝者を先に焼く
    winner = OutOfScopeArticleRecordORM(
        curation_id=extraction.id,
        translated_title="winner title",
        summary="winner summary",
        investor_take="not relevant",
    )
    db_session.add(winner)
    await db_session.commit()

    assessor = _make_assessor(return_envelope=_out_of_scope_call())
    svc = AssessmentService(session_factory)
    result = await svc.execute(
        _ready(extraction),
        assessor,
        analyzable_article_id=article.id,
        logger=assessment_logger,
    )

    assert result == AssessmentCompletion(AssessmentCompletionKind.ALREADY_ASSESSED)
    async with session_factory() as reader:
        rows = (
            await reader.execute(
                select(
                    OutOfScopeArticleRecordORM.translated_title,
                    OutOfScopeArticleRecordORM.summary,
                ).where(OutOfScopeArticleRecordORM.curation_id == extraction.id)
            )
        ).all()
    assert [tuple(row) for row in rows] == [("winner title", "winner summary")]


# processing_outcome emit — commit 後に in_scope/out_of_scope、race loss は emit しない


@pytest.mark.asyncio
async def test_in_scope_emits_processing_outcome_in_scope(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    capfire: CaptureLogfire,
    assessment_logger,
) -> None:
    """in_scope 保存 + commit 後に processing_outcome{result=in_scope} が +1。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_in_scope_call(InScopeCategory.AI))

    svc = AssessmentService(session_factory)
    await svc.execute(
        _ready(extraction),
        assessor,
        analyzable_article_id=article.id,
        logger=assessment_logger,
    )

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, "in_scope") == 1
    assert (
        sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, "out_of_scope") == 0
    )


@pytest.mark.asyncio
async def test_out_of_scope_emits_processing_outcome_out_of_scope(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capfire: CaptureLogfire,
    assessment_logger,
) -> None:
    """out_of_scope 保存 + commit 後に out_of_scope が +1 される。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_out_of_scope_call())

    svc = AssessmentService(session_factory)
    await svc.execute(
        _ready(extraction),
        assessor,
        analyzable_article_id=article.id,
        logger=assessment_logger,
    )

    metrics = collected_metrics(capfire)
    assert (
        sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, "out_of_scope") == 1
    )
    assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, "in_scope") == 0


@pytest.mark.asyncio
async def test_race_loss_does_not_emit_processing_outcome(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    capfire: CaptureLogfire,
    assessment_logger,
) -> None:
    """楽観ロック敗北 (commit 未到達) では processing_outcome を emit しない。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_in_scope_call(InScopeCategory.AI))

    svc = AssessmentService(session_factory)
    with patch(
        "app.analysis.assessment.repository.AssessmentRepository.save_in_scope",
        new=AsyncMock(return_value=None),
    ):
        await svc.execute(
            _ready(extraction),
            assessor,
            analyzable_article_id=article.id,
            logger=assessment_logger,
        )

    metrics = collected_metrics(capfire)
    for result in ("in_scope", "out_of_scope", "failed"):
        assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, result) == 0


@pytest.mark.asyncio
async def test_in_scope_persists_result_and_matching_outbox_event(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    assessment_logger,
) -> None:
    article = await _make_article(db_session, sample_source)
    curation = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_in_scope_call())

    completion = await AssessmentService(session_factory).execute(
        _ready(curation),
        assessor,
        analyzable_article_id=article.id,
        logger=assessment_logger,
    )

    async with session_factory() as reader:
        saved = (
            await reader.execute(
                select(AnalyzedArticleRecordORM).where(
                    AnalyzedArticleRecordORM.curation_id == curation.id
                )
            )
        ).scalar_one()
        event = (
            await reader.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == ArticleAssessedInScope.EVENT_TYPE
                )
            )
        ).scalar_one()

        assert completion == AssessmentCompletion(
            AssessmentCompletionKind.IN_SCOPE, saved.id
        )
        assert event.payload == {
            "curation_id": curation.id,
            "analyzed_article_id": saved.id,
        }
        assert event.schema_version == ArticleAssessedInScope.SCHEMA_VERSION


@pytest.mark.asyncio
async def test_outbox_insert_failure_rolls_back_in_scope_result(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    reject_outbox_insert: RejectOutboxInsert,
    assessment_logger,
) -> None:
    article = await _make_article(db_session, sample_source)
    curation = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_in_scope_call())
    constraint_name = await reject_outbox_insert(ArticleAssessedInScope.EVENT_TYPE)

    with pytest.raises(IntegrityError, match=constraint_name):
        await AssessmentService(session_factory).execute(
            _ready(curation),
            assessor,
            analyzable_article_id=article.id,
            logger=assessment_logger,
        )

    async with session_factory() as reader:
        results = (
            (
                await reader.execute(
                    select(AnalyzedArticleRecordORM.id).where(
                        AnalyzedArticleRecordORM.curation_id == curation.id
                    )
                )
            )
            .scalars()
            .all()
        )
        outbox_events = (
            (await reader.execute(select(OutboxEvent.event_id))).scalars().all()
        )
    assert {
        "results": results,
        "outbox_events": outbox_events,
    } == {"results": [], "outbox_events": []}


@pytest.mark.asyncio
async def test_out_of_scope_writes_no_outbox_event(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    assessment_logger,
) -> None:
    article = await _make_article(db_session, sample_source)
    curation = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_out_of_scope_call())

    completion = await AssessmentService(session_factory).execute(
        _ready(curation),
        assessor,
        analyzable_article_id=article.id,
        logger=assessment_logger,
    )

    async with session_factory() as reader:
        saved_curation_id = (
            await reader.execute(
                select(OutOfScopeArticleRecordORM.curation_id).where(
                    OutOfScopeArticleRecordORM.curation_id == curation.id
                )
            )
        ).scalar_one()
        events = (await reader.execute(select(OutboxEvent.event_id))).scalars().all()
    assert saved_curation_id == curation.id
    assert events == []

    assert completion == AssessmentCompletion(AssessmentCompletionKind.OUT_OF_SCOPE)


@pytest.mark.asyncio
@pytest.mark.parametrize("in_scope", [True, False], ids=["in_scope", "out_of_scope"])
async def test_duplicate_save_returns_already_assessed_without_commit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    in_scope: bool,
    assessment_logger,
) -> None:
    """Repositoryが実DBで重複を検出すると、Serviceはcommitせず判定済みを返す。"""
    article = await _make_article(db_session, sample_source)
    curation = await _make_extraction(db_session, article)
    ready = _ready(curation)
    assessor = _make_assessor(
        return_envelope=_in_scope_call() if in_scope else _out_of_scope_call()
    )
    service = AssessmentService(session_factory)
    first = await service.execute(
        ready, assessor, analyzable_article_id=article.id, logger=assessment_logger
    )

    assert first.kind is (
        AssessmentCompletionKind.IN_SCOPE
        if in_scope
        else AssessmentCompletionKind.OUT_OF_SCOPE
    )

    with patch.object(AsyncSession, "commit", new_callable=AsyncMock) as commit:
        repeated = await service.execute(
            ready, assessor, analyzable_article_id=article.id, logger=assessment_logger
        )
        commit.assert_not_awaited()

    assert repeated == AssessmentCompletion(AssessmentCompletionKind.ALREADY_ASSESSED)
