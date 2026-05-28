"""``AssessmentAuditRepository`` の semantic method 単独テスト。

audit row の shape SSoT が repository に集約されたことを検証する:

- ``append_in_scope`` で
  ``outcome_code="assessed_in_scope"`` + payload に
  ``category_slug`` / ``investor_take`` 詰まる
  (``category_slug`` は ``in_scope.category.value`` から Repository 内で導出、
  ``raw_category`` envelope 由来とは独立)
- ``append_out_of_scope`` で
  ``outcome_code="assessed_out_of_scope"`` + payload に
  ``investor_take`` が非 None
  (PR #447 対称化追従、in-scope 固有 field の ``category_slug`` のみ None)
- ``append_failure`` で **exc 型による 3 dispatch + Layer 2-B + catch-all** が動作:
  - ``AssessmentRecoverableError`` → ``retryability=retryable``
  - ``AssessmentTerminalStageBlockedError`` → ``retryability=non_retryable``
  - ``AssessmentResponseInvalidError`` (Layer 2-B) → ``retryability=retryable`` /
    ``outcome_code="assessment_response_invalid"``
  - ``AssessmentCategoryMissingError`` (Layer 2-B) →
    ``retryability=non_retryable`` / ``outcome_code="assessment_category_missing"``
  - 想定外 ``RuntimeError`` → ``retryability=unknown`` /
    ``outcome_code="unexpected_error"``
- ``error_chain`` が ``__cause__`` 経由で 2 段以上を記録
- ``error_message`` が ``redact_secrets()`` 経由
- ``ai_raw_response`` が成功・失敗両経路で ``[:_AI_RAW_RESPONSE_LIMIT]`` 切詰
- repository は ``commit`` を呼ばない (caller の tx 境界保持)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildBlocked,
    AssessmentReadyBuildBlockedCode,
    ReadyForAssessment,
)
from app.analysis.assessment.domain.result import (
    InScope,
    InScopeCategory,
    OutOfScope,
)
from app.analysis.assessment.errors import (
    AssessmentCategoryMissingError,
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalStageBlockedError,
    AssessmentTerminalTargetRejectedError,
)
from app.audit.stages.assessment import AssessmentAuditRepository
from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import BackfillExclusionReason
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment as InScopeAssessmentORM
from app.models.news_source import NewsSource
from app.models.out_of_scope_assessment import (
    OutOfScopeAssessment as OutOfScopeAssessmentORM,
)
from app.models.pipeline_event import PipelineEvent

_AI_MODEL = "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# 補助 fixture: extraction を作る (article_id 逆引き経路を一貫性ある状態にする)
# ---------------------------------------------------------------------------


async def _make_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/a",
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="t",
        original_content="c" * 100,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


async def _make_extraction(
    db_session: AsyncSession,
    article: Article,
    *,
    summary: str = "summary text",
) -> ArticleCuration:
    extraction = ArticleCuration(
        article_id=article.id,
        translated_title="title",
        summary=summary,
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


def _ready(
    extraction: ArticleCuration,
    *,
    summary: str | None = None,
    source_name: str | None = "Test Source",
) -> ReadyForAssessment:
    return ReadyForAssessment(
        curation_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=summary if summary is not None else extraction.summary,
        article_id=extraction.article_id,
        source_name=source_name,
    )


def _make_in_scope(category: InScopeCategory = InScopeCategory.AI) -> InScope:
    """``append_in_scope`` に渡す AI 境界型を組み立てる。"""
    return InScope(
        category=category,
        investor_take="bullish",
    )


def _in_scope_call(
    *,
    raw_response: str = '{"category":"ai"}',
    raw_category: str = "ai",
    in_scope: InScope | None = None,
) -> AssessmentCall[InScope]:
    return AssessmentCall(
        result=in_scope if in_scope is not None else _make_in_scope(),
        raw_response=raw_response,
        raw_category=raw_category,
        prompt_version="abcd1234",
        model_name=_AI_MODEL,
    )


def _out_of_scope_call() -> AssessmentCall[OutOfScope]:
    return AssessmentCall(
        result=OutOfScope(investor_take="not relevant"),
        raw_response='{"category":"out_of_scope"}',
        raw_category="out_of_scope",
        prompt_version="abcd1234",
        model_name=_AI_MODEL,
    )


async def _persist_in_scope(
    db_session: AsyncSession,
    extraction: ArticleCuration,
    category: Category,
) -> InScopeAssessmentORM:
    """テスト用に in_scope_assessments 行を 1 件焼いて ORM を返す。

    audit は witness で ID ミラーを持たないが、article_id 経由の 1-hop join
    で audit row を引くための業務 row を焼くために使う。
    """
    orm = InScopeAssessmentORM(
        curation_id=extraction.id,
        translated_title="title",
        summary="summary text",
        category_id=category.id,
        investor_take="bullish",
    )
    db_session.add(orm)
    await db_session.commit()
    await db_session.refresh(orm)
    return orm


async def _persist_out_of_scope(
    db_session: AsyncSession,
    extraction: ArticleCuration,
) -> OutOfScopeAssessmentORM:
    """テスト用に out_of_scope_assessments 行を 1 件焼いて ORM を返す。"""
    orm = OutOfScopeAssessmentORM(
        curation_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=extraction.summary,
        investor_take="not relevant",
    )
    db_session.add(orm)
    await db_session.commit()
    await db_session.refresh(orm)
    return orm


async def _fetch_one(db_session: AsyncSession, article_id: int) -> PipelineEvent:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    return rows[0]


async def _fetch_by_outcome(
    db_session: AsyncSession, outcome_code: str
) -> PipelineEvent:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.outcome_code == outcome_code)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# 成功経路 — append_in_scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_ready_build_blocked_records_missing_curation_rejected(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ready build blocked は rejected として curation_id を payload に残す。"""
    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_ready_build_blocked(
            blocked=AssessmentReadyBuildBlocked(
                curation_id=999,
                code=AssessmentReadyBuildBlockedCode.CURATION_MISSING,
            )
        )
        await session.commit()

    ev = await _fetch_by_outcome(
        db_session, "assessment_ready_build_blocked_curation_missing"
    )
    assert ev.event_type == "rejected"
    assert ev.article_id is None
    assert ev.payload["curation_id"] == 999


@pytest.mark.asyncio
async def test_append_ready_build_failed_records_unknown_failure(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ready build failed は failed / unknown retryability で trigger id を残す。"""
    exc = RuntimeError("ready build exploded")
    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_ready_build_failed(
            curation_id=123,
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_by_outcome(
        db_session, "assessment_ready_build_failed_unexpected_error"
    )
    assert ev.event_type == "failed"
    assert ev.retryability == "unknown"
    assert ev.error_class == "builtins.RuntimeError"
    assert ev.payload["failure_kind"] == "unexpected_error"
    assert ev.payload["curation_id"] == 123


@pytest.mark.asyncio
async def test_append_in_scope_records_success_with_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """succeeded / outcome_code=assessed_in_scope と payload の主要 field を確認。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    await _persist_in_scope(db_session, extraction, sample_categories[0])

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            call=_in_scope_call(),
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "assessed_in_scope"
    assert ev.retryability is None
    assert ev.payload["curation_id"] == extraction.id
    assert ev.payload["investor_take"] == "bullish"
    assert ev.payload["ai_model"] == _AI_MODEL


@pytest.mark.asyncio
async def test_append_in_scope_derives_category_slug_from_in_scope(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """``category_slug`` は ``in_scope.category.value`` から Repository 内で導出。

    ``raw_category`` (envelope 由来、validation 前生値) と意味分離されて
    格納されることを固定する (caller は固定文字列を渡さない)。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    await _persist_in_scope(db_session, extraction, sample_categories[0])
    in_scope_response = _make_in_scope(category=InScopeCategory.AI)
    # call.raw_category と category_slug を区別するため envelope 側だけ異常値
    call = _in_scope_call(
        in_scope=in_scope_response,
        raw_category="ai_raw_from_envelope",
    )

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            call=call,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["raw_category"] == "ai_raw_from_envelope"  # call 由来
    assert ev.payload["category_slug"] == "ai"  # in_scope.category.value 由来


@pytest.mark.asyncio
async def test_append_in_scope_uses_article_id_from_ready(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """pipeline_events.article_id が Ready 由来 (案 3: Ready 構築時に取得済)。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    await _persist_in_scope(db_session, extraction, sample_categories[0])

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            call=_in_scope_call(),
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.article_id == article.id  # ready.article_id を直接利用


@pytest.mark.asyncio
async def test_append_in_scope_uses_source_name_from_ready(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """payload.source_name が Ready 由来 (案 3: 2-hop 逆引き撤去)。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    await _persist_in_scope(db_session, extraction, sample_categories[0])

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction, source_name=str(sample_source.name)),
            call=_in_scope_call(),
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["source_name"] == str(sample_source.name)


@pytest.mark.asyncio
async def test_append_in_scope_does_not_commit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """repository は session.commit() を呼ばない (caller tx 境界保持)。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    await _persist_in_scope(db_session, extraction, sample_categories[0])

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            call=_in_scope_call(),
        )
        # 意図的に commit しない (rollback で消える)

    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0  # 未 commit のため永続化されていない


@pytest.mark.asyncio
async def test_append_in_scope_truncates_raw_response(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """envelope.raw_response が 2KB 超なら _AI_RAW_RESPONSE_LIMIT で切詰。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    await _persist_in_scope(db_session, extraction, sample_categories[0])
    huge_raw = "x" * 5000
    call = _in_scope_call(raw_response=huge_raw)

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            call=call,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["ai_raw_response"] is not None
    assert len(ev.payload["ai_raw_response"]) == 2048  # _AI_RAW_RESPONSE_LIMIT


# ---------------------------------------------------------------------------
# 成功経路 — append_out_of_scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_out_of_scope_records_success_with_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """succeeded / outcome_code=assessed_out_of_scope。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    await _persist_out_of_scope(db_session, extraction)

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_out_of_scope(
            ready=_ready(extraction),
            call=_out_of_scope_call(),
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "assessed_out_of_scope"
    assert ev.retryability is None


@pytest.mark.asyncio
async def test_append_out_of_scope_records_investor_take(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """PR #447 対称化追従: out-of-scope payload は ``investor_take`` を持つ。

    本体 DB (``out_of_scope_assessments.investor_take``) と audit payload の
    情報量を一致させる。in-scope 固有 field (``category_slug``) のみ None。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    out_of_scope = await _persist_out_of_scope(db_session, extraction)

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_out_of_scope(
            ready=_ready(extraction),
            call=_out_of_scope_call(),
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["investor_take"] == out_of_scope.investor_take  # 非 None
    # in-scope 固有 field のみ None
    assert ev.payload["category_slug"] is None


# ---------------------------------------------------------------------------
# 救済断念経路 — append_backfill_assessment_aged_out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_backfill_assessment_aged_out_records_rejected(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """backfill age-out exclusion を rejected event として記録する。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_backfill_assessment_aged_out(
            curation_id=extraction.id,
            article_id=article.id,
            source_name=str(sample_source.name),
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.stage == "backfill_assess"
    assert ev.event_type == "rejected"
    assert ev.outcome_code == BackfillExclusionReason.ASSESSMENT_AGED_OUT.value
    assert ev.retryability is None
    assert ev.payload["kind"] == "assessment"
    assert ev.payload["source_name"] == str(sample_source.name)
    assert ev.payload["curation_id"] == extraction.id


# ---------------------------------------------------------------------------
# 失敗経路 — append_failure (Layer 1 marker dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_failure_recoverable_maps_to_retryable(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AssessmentRecoverableError → retryable / outcome_code=instance attr。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = AssessmentRecoverableError(code="ai_error_network")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_network"
    assert ev.retryability == "retryable"
    assert ev.payload["failure_kind"] == "recoverable"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_append_failure_terminal_stage_blocked(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AssessmentTerminalStageBlockedError → non_retryable / stage_blocked。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = AssessmentTerminalStageBlockedError(code="ai_error_configuration")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == "ai_error_configuration"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_stage_blocked"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_append_failure_terminal_target_rejected(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AssessmentTerminalTargetRejectedError → non_retryable / target_rejected。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = AssessmentTerminalTargetRejectedError(code="ai_error_input_rejected")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == "ai_error_input_rejected"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_target_rejected"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_append_failure_layer_2b_response_invalid(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Layer 2-B AssessmentResponseInvalidError は Recoverable 継承で retryable。

    ctor は message のみ、code は内部で hardcode (assessment_response_invalid)。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = AssessmentResponseInvalidError()

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == "assessment_response_invalid"
    assert ev.retryability == "retryable"
    assert ev.payload["failure_kind"] == "recoverable"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_append_failure_layer_2b_category_missing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Layer 2-B AssessmentCategoryMissingError は分類未解決として記録される。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = AssessmentCategoryMissingError()

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == "assessment_category_missing"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_classification_unresolved"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_append_failure_unknown_exception_maps_to_unknown(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """非 marker exception は unknown / outcome_code=unexpected_error。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = RuntimeError("boom")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_unexpected_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == "unexpected_error"
    assert ev.retryability == "unknown"
    assert ev.payload["failure_kind"] == "unknown"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "exc_factory",
        "expected_outcome_code",
        "expected_retryability",
        "expected_failure_kind",
    ),
    [
        # 外部 DB 例外は classify_db_error adapter で意味ラベルに分類される
        # (SQLAlchemy が振る .code=gkpj 等を拾わない)。
        (
            lambda: OperationalError("SELECT 1", {}, Exception("conn reset")),
            "db_runtime_error",
            "retryable",
            "db_runtime",
        ),
        (
            lambda: IntegrityError("INSERT", {}, Exception("unique violation")),
            "db_constraint_error",
            "non_retryable",
            "db_constraint",
        ),
        (
            lambda: ProgrammingError("SELECT bad", {}, Exception("no such column")),
            "db_query_or_schema_error",
            "non_retryable",
            "db_query_or_schema",
        ),
        (
            lambda: InvalidRequestError("detached instance"),
            "db_unknown_error",
            "unknown",
            "db_unknown",
        ),
    ],
)
async def test_append_failure_projects_db_exceptions(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    exc_factory: object,
    expected_outcome_code: str,
    expected_retryability: str,
    expected_failure_kind: str,
) -> None:
    """SQLAlchemy DB 例外を failure projection に分類する。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = exc_factory()  # type: ignore[operator]

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == expected_outcome_code
    assert ev.retryability == expected_retryability
    assert ev.payload["failure_kind"] == expected_failure_kind
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_append_failure_walks_error_chain_via_cause(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """error_chain は __cause__ を辿り 2 段以上を記録する (PR6 wrapper raise 想定)。

    PR6 で ``raise map_provider_to_assessment(exc) from exc`` が走ると
    Layer 1 marker (wrapper) と元 ``AIProvider*Error`` の両方が必要。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    try:
        try:
            raise RuntimeError("upstream provider error")
        except RuntimeError as inner:
            # Phase 4: kwargs-only constructor。message 引数廃止 (PII 隔離契約)。
            raise AssessmentRecoverableError(code="ai_error_network") from inner
    except AssessmentRecoverableError as exc:
        async with session_factory() as session:
            await AssessmentAuditRepository(session).append_failure(
                ready=_ready(extraction),
                exc=exc,
            )
            await session.commit()

    ev = await _fetch_one(db_session, article.id)
    chain = ev.payload["error_chain"]
    assert chain is not None
    assert len(chain) >= 2
    assert chain[0].endswith(".AssessmentRecoverableError")
    assert chain[1].endswith(".RuntimeError")


@pytest.mark.asyncio
async def test_append_failure_redacts_secrets_in_error_message(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """error_message が redact_secrets() 経由で 永続化される (red-team chain γ-2)。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = RuntimeError(
        "Authorization: Bearer "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2QT4abc failed"
    )

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_unexpected_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["error_message"] is not None
    assert "SflKxwRJSMeKKF2QT4abc" not in ev.payload["error_message"]
    assert "***" in ev.payload["error_message"]


@pytest.mark.asyncio
async def test_append_failure_truncates_raw_response_attr(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """exc に raw_response instance attr が乗ったら 2KB 切詰 (成功経路と対称)。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc: Any = RuntimeError("schema mismatch")
    exc.raw_response = "x" * 5000

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_unexpected_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["ai_raw_response"] is not None
    assert len(ev.payload["ai_raw_response"]) == 2048


@pytest.mark.asyncio
async def test_append_failure_records_curation_id_in_payload(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """payload.curation_id が ready.curation_id と一致する。

    Stage 4 固有 identifier (top-level column が無いため payload で保持)。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = RuntimeError("boom")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_unexpected_failure(
            ready=_ready(extraction),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["curation_id"] == extraction.id
