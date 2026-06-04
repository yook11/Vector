"""``AssessmentService`` の PR6 改修 test (audit wire-in + ACL boundary)。

PR6 で Service が以下を行うようになったことを固定する:

- ``assessor.assess`` の ``AIProviderError`` を ``map_provider_to_assessment``
  で Stage 4 marker (``AssessmentRecoverableError`` /
  ``AssessmentTerminalStageBlockedError``) に詰め替え、``__cause__`` に元
  ``AIProvider*Error`` を紐付ける (ACL boundary)。
- ``_handle_in_scope`` で category 解決失敗 (``category_id is None``) のとき
  ``CategoryEnumDatabaseMismatchError`` (enum↔DB 不整合) を raise する。
- 業務 INSERT (in-scope / out-of-scope) と同 session 同 tx で
  ``AssessmentAuditRepository.append_*`` を呼び、成功 audit を 1 行焼く。
- race lost (``save()`` が None) の場合は audit を焼かず ``None`` を返す
  (actor SSoT、勝者 task の audit と二重記録しない、再収集は reconcile cron 経路)。

PR5 で merge 済の repository / payload / errors (Layer 2-A ACL) は本 PR では
touch しない (test では結果として焼かれた pipeline_events 行を assert するに留める)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
)
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.domain.result import (
    InScope,
    InScopeCategory,
    OutOfScope,
)
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentTerminalStageBlockedError,
)
from app.analysis.assessment.repository import CategoryEnumDatabaseMismatchError
from app.analysis.assessment.service import AssessmentService
from app.logfire.article_stage import assessment_stage_span
from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment as InScopeAssessmentORM
from app.models.news_source import NewsSource
from app.models.out_of_scope_assessment import (
    OutOfScopeAssessment as OutOfScopeAssessmentORM,
)
from app.models.pipeline_event import PipelineEvent
from tests.logfire._span_helpers import stage_attrs

_AI_MODEL = "gemini-2.5-flash-lite"


# Helpers (test_assessment_audit_repository.py と同じ pattern)


async def _make_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/a",
) -> Article:
    article = Article(
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
    db_session: AsyncSession, article: Article
) -> ArticleCuration:
    extraction = ArticleCuration(
        article_id=article.id,
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
        article_id=extraction.article_id,
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


async def _fetch_assessment_events(
    db_session: AsyncSession, article_id: int
) -> list[PipelineEvent]:
    stmt = (
        select(PipelineEvent)
        .where(
            PipelineEvent.article_id == article_id,
            PipelineEvent.stage == "assessment",
        )
        .order_by(PipelineEvent.id)
    )
    return list((await db_session.execute(stmt)).scalars().all())


# 成功経路: 業務 INSERT と同 tx で audit 1 行


@pytest.mark.asyncio
async def test_in_scope_success_records_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """``_handle_in_scope`` 成功で ``outcome_code=assessed_in_scope`` の audit 1 行。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    # InScopeCategory.AI が catalog に存在する slug "ai" になることを前提
    assessor = _make_assessor(
        return_envelope=_in_scope_call(category=InScopeCategory.AI)
    )

    svc = AssessmentService(session_factory)
    result = await svc.execute(_ready(extraction), assessor)
    # in-scope 成功時 Service は assessment id (int) を返す
    assert isinstance(result, int) and result > 0

    events = await _fetch_assessment_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "assessed_in_scope"
    assert ev.retryability is None
    payload = ev.payload
    assert payload["curation_id"] == extraction.id
    assert payload["investor_take"] == "bullish"
    assert payload["ai_model"] == _AI_MODEL
    assert payload["category_slug"] == "ai"


@pytest.mark.asyncio
async def test_out_of_scope_success_records_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """out-of-scope 成功で ``outcome_code=assessed_out_of_scope`` の audit 1 行。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_out_of_scope_call())

    svc = AssessmentService(session_factory)
    result = await svc.execute(_ready(extraction), assessor)
    # out-of-scope は Stage 5 chain しないため Service は None を返す
    assert result is None

    events = await _fetch_assessment_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "assessed_out_of_scope"
    assert ev.retryability is None
    payload = ev.payload
    assert payload["curation_id"] == extraction.id
    # PR #447 対称化追従: investor_take は本体 DB と一致 (非 None)
    assert payload.get("investor_take") == "not relevant"
    # in-scope 固有 field のみ None (category_slug)
    assert payload.get("category_slug") is None
    # 本体 DB (out_of_scope_assessments) に Stage 3 由来 snapshot が永続化されている
    persisted = (
        await db_session.execute(
            select(OutOfScopeAssessmentORM).where(
                OutOfScopeAssessmentORM.curation_id == extraction.id
            )
        )
    ).scalar_one()
    assert persisted.translated_title == extraction.translated_title
    assert persisted.summary == extraction.summary


# race lost: audit を焼かない (actor SSoT)


@pytest.mark.asyncio
async def test_race_lost_does_not_record_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """``InScopeRepository.save`` が None (race lost) のとき audit 行は 0。

    敗者は ``None`` を返し audit も焼かない (勝者 task の audit と二重記録しない、
    救済は reconcile cron に委譲)。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    # 勝者 row を先に焼いておく (本 test は敗者経路)
    winner = InScopeAssessmentORM(
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
    # 敗者経路では save_in_scope() が None → Service も None を返して短絡する
    with patch(
        "app.analysis.assessment.repository.AssessmentRepository.save_in_scope",
        new=AsyncMock(return_value=None),
    ):
        result = await svc.execute(_ready(extraction), assessor)

    assert result is None
    # race lost 経路では audit 行はゼロ (actor SSoT を assert)
    events = await _fetch_assessment_events(db_session, article.id)
    assert len(events) == 0


# ACL boundary: AIProviderError → Stage 4 marker wrap


@pytest.mark.asyncio
async def test_provider_network_error_is_wrapped_to_recoverable_marker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``AIProviderNetworkError`` → ``AssessmentRecoverableError`` で wrap。

    ``__cause__`` に元 ``AIProvider*Error`` が紐付くこと (PR5 の
    ``extract_error_chain`` が 2 段以上を error_chain 列に記録できる前提)。
    """
    provider_exc = AIProviderNetworkError("connection reset")
    assessor = _make_assessor(side_effect=provider_exc)

    ready = ReadyForAssessment(
        curation_id=1,
        translated_title="t",
        summary="s",
        article_id=1,
    )
    svc = AssessmentService(session_factory)

    with pytest.raises(AssessmentRecoverableError) as excinfo:
        await svc.execute(ready, assessor)
    assert excinfo.value.__cause__ is provider_exc
    assert excinfo.value.provider_error is provider_exc


@pytest.mark.asyncio
async def test_provider_configuration_error_is_wrapped_to_stage_blocked_marker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``AIProviderConfigurationError`` → ``AssessmentTerminalStageBlockedError``。"""
    provider_exc = AIProviderConfigurationError("bad api key")
    assessor = _make_assessor(side_effect=provider_exc)

    ready = ReadyForAssessment(
        curation_id=1,
        translated_title="t",
        summary="s",
        article_id=1,
    )
    svc = AssessmentService(session_factory)

    with pytest.raises(AssessmentTerminalStageBlockedError) as excinfo:
        await svc.execute(ready, assessor)
    assert excinfo.value.__cause__ is provider_exc
    assert excinfo.value.provider_error is provider_exc


# enum↔DB 不整合: catalog 未登録 slug → CategoryEnumDatabaseMismatchError


@pytest.mark.asyncio
async def test_unknown_category_raises_enum_db_mismatch(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
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
        await svc.execute(_ready(extraction), assessor)
    assert excinfo.value.missing == {"ai"}


@pytest.mark.asyncio
async def test_unknown_category_does_not_record_audit_in_service(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """``CategoryEnumDatabaseMismatchError`` 経路でも Service が audit を焼かない
    (失敗 audit は Task 層末尾の inline audit ブロックが別 session で焼く責務)。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(
        return_envelope=_in_scope_call(category=InScopeCategory.AI)
    )

    svc = AssessmentService(session_factory)
    with pytest.raises(CategoryEnumDatabaseMismatchError):
        await svc.execute(_ready(extraction), assessor)

    events = await _fetch_assessment_events(db_session, article.id)
    assert len(events) == 0


# 同 tx 性: 業務 INSERT が rollback されると audit も焼かれない


@pytest.mark.asyncio
async def test_audit_rolled_back_when_commit_fails(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """``session.commit`` が raise すると業務 INSERT も audit も両方残らない
    (同 session 同 tx の原子性)。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(
        return_envelope=_in_scope_call(category=InScopeCategory.AI)
    )

    svc = AssessmentService(session_factory)
    # AsyncSession.commit() を patch して、append_in_scope の後に raise
    boom = RuntimeError("commit failed")
    with patch(
        "sqlalchemy.ext.asyncio.AsyncSession.commit",
        new=AsyncMock(side_effect=boom),
    ):
        with pytest.raises(RuntimeError, match="commit failed"):
            await svc.execute(_ready(extraction), assessor)

    # audit も業務 in_scope_assessments も両方ゼロ (同 tx で rollback)
    events = await _fetch_assessment_events(db_session, article.id)
    assert len(events) == 0
    rows = (
        (
            await db_session.execute(
                select(InScopeAssessmentORM).where(
                    InScopeAssessmentORM.curation_id == extraction.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0


# Race lost on out-of-scope path


@pytest.mark.asyncio
async def test_out_of_scope_race_lost_does_not_record_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """out-of-scope 経路の race lost でも audit は焼かれない。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    # 勝者を先に焼く
    winner = OutOfScopeAssessmentORM(
        curation_id=extraction.id,
        translated_title="title",
        summary="summary text",
        investor_take="not relevant",
    )
    db_session.add(winner)
    await db_session.commit()

    assessor = _make_assessor(return_envelope=_out_of_scope_call())
    svc = AssessmentService(session_factory)
    with patch(
        "app.analysis.assessment.repository.AssessmentRepository.save_out_of_scope",
        new=AsyncMock(return_value=None),
    ):
        result = await svc.execute(_ready(extraction), assessor)

    assert result is None
    events = await _fetch_assessment_events(db_session, article.id)
    assert len(events) == 0


# article_stage span: 実 Service が各分岐で result 語彙を焼く正本


@pytest.mark.asyncio
async def test_in_scope_sets_stage_result_in_scope(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    capfire: CaptureLogfire,
) -> None:
    """in-scope 保存成功で active span に result=in_scope が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_in_scope_call(InScopeCategory.AI))

    svc = AssessmentService(session_factory)
    with assessment_stage_span(curation_id=extraction.id):
        await svc.execute(_ready(extraction), assessor)

    assert stage_attrs(capfire)["result"] == "in_scope"


@pytest.mark.asyncio
async def test_out_of_scope_sets_stage_result_out_of_scope(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """out-of-scope 保存成功で active span に result=out_of_scope が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_out_of_scope_call())

    svc = AssessmentService(session_factory)
    with assessment_stage_span(curation_id=extraction.id):
        await svc.execute(_ready(extraction), assessor)

    assert stage_attrs(capfire)["result"] == "out_of_scope"


@pytest.mark.asyncio
async def test_race_loss_sets_stage_result_skipped(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    capfire: CaptureLogfire,
) -> None:
    """in-scope の楽観ロック敗北 (save_in_scope=None) で result=skipped が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    assessor = _make_assessor(return_envelope=_in_scope_call(InScopeCategory.AI))

    svc = AssessmentService(session_factory)
    with assessment_stage_span(curation_id=extraction.id):
        with patch(
            "app.analysis.assessment.repository.AssessmentRepository.save_in_scope",
            new=AsyncMock(return_value=None),
        ):
            await svc.execute(_ready(extraction), assessor)

    assert stage_attrs(capfire)["result"] == "skipped"
