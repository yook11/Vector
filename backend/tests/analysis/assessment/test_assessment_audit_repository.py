"""``AssessmentAuditRepository`` の semantic method 単独テスト (PR5)。

audit row の shape SSoT が repository に集約されたことを検証する:

- ``append_in_scope`` で
  ``category=success`` + ``code="assessed_in_scope"`` + payload に
  ``category_id`` / ``category_slug`` / ``topic`` / ``investor_take`` 詰まる
- ``append_out_of_scope`` で
  ``category=success`` + ``code="assessed_out_of_scope"`` + payload に
  ``assessment_id`` のみ非 None (in-scope 系 4 field は全て None)
- ``append_failure`` で **exc 型による 3 dispatch + Layer 2-B + catch-all** が動作:
  - ``AssessmentRecoverableError`` → ``category=retryable``
  - ``AssessmentTerminalSkipError`` → ``category=non_retryable_keep_extraction``
  - ``AssessmentResponseInvalidError`` (Layer 2-B) → ``retryable`` /
    ``code="assessment_response_invalid"``
  - ``AssessmentCategoryMissingError`` (Layer 2-B) →
    ``non_retryable_keep_extraction`` / ``code="assessment_category_missing"``
  - 想定外 ``RuntimeError`` → ``category=unknown`` / ``code="unexpected_error"``
- ``error_chain`` が ``__cause__`` 経由で 2 段以上を記録 (PR6 で
  ``raise X from exc`` 想定)
- ``error_message`` が ``redact_secrets()`` 経由
- ``ai_raw_response`` が成功・失敗両経路で ``[:_AI_RAW_RESPONSE_LIMIT]`` 切詰
- ``category_slug`` (caller 渡し) と ``raw_category`` (envelope 由来) が独立
- repository は ``commit`` を呼ばない (caller の tx 境界保持)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.assessment.audit_repository import AssessmentAuditRepository
from app.analysis.assessment.domain.in_scope import InScopeAssessment
from app.analysis.assessment.domain.out_of_scope import OutOfScopeAssessment
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import (
    AssessmentCategoryMissingError,
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalSkipError,
)
from app.analysis.classifier.envelope import AssessmentCall
from app.analysis.classifier.schema import (
    InScope,
    InScopeCategory,
    OutOfScope,
)
from app.analysis.domain.value_objects.topic import TopicName
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
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
) -> ArticleExtraction:
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="title",
        summary=summary,
        ai_model=_AI_MODEL,
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


def _ready(
    extraction: ArticleExtraction, *, summary: str | None = None
) -> ReadyForAssessment:
    return ReadyForAssessment(
        extraction_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=summary if summary is not None else extraction.summary,
    )


def _in_scope_envelope(*, raw_response: str = '{"category":"ai"}') -> AssessmentCall:
    return AssessmentCall(
        result=InScope(
            category=InScopeCategory.AI,
            topic=TopicName("llm benchmark"),
            investor_take="bullish",
        ),
        raw_response=raw_response,
        raw_category="ai",
        raw_topic="LLM Benchmark",
        prompt_version="abcd1234",
    )


def _out_of_scope_envelope() -> AssessmentCall:
    return AssessmentCall(
        result=OutOfScope(investor_take="not relevant"),
        raw_response='{"category":"out_of_scope"}',
        raw_category="out_of_scope",
        raw_topic="celebrity gossip",
        prompt_version="abcd1234",
    )


async def _persist_in_scope(
    db_session: AsyncSession,
    extraction: ArticleExtraction,
    category: Category,
) -> InScopeAssessment:
    orm = InScopeAssessmentORM(
        extraction_id=extraction.id,
        translated_title="title",
        summary="summary text",
        topic="llm benchmark",
        category_id=category.id,
        investor_take="bullish",
        ai_model=_AI_MODEL,
    )
    db_session.add(orm)
    await db_session.commit()
    await db_session.refresh(orm)
    # ORM の TopicName custom type が VO ↔ str を双方向変換するため
    # ``orm.topic`` は既に ``TopicName`` instance。再 wrap しない。
    return InScopeAssessment(
        id=orm.id,
        extraction_id=orm.extraction_id,
        translated_title=orm.translated_title,
        summary=orm.summary,
        topic=orm.topic,
        category_id=orm.category_id,
        investor_take=orm.investor_take,
        ai_model=orm.ai_model,
        analyzed_at=orm.analyzed_at,
    )


async def _persist_out_of_scope(
    db_session: AsyncSession,
    extraction: ArticleExtraction,
) -> OutOfScopeAssessment:
    orm = OutOfScopeAssessmentORM(
        extraction_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=extraction.summary,
        investor_take="not relevant",
        ai_model=_AI_MODEL,
    )
    db_session.add(orm)
    await db_session.commit()
    await db_session.refresh(orm)
    return OutOfScopeAssessment(
        id=orm.id,
        extraction_id=orm.extraction_id,
        translated_title=orm.translated_title,
        summary=orm.summary,
        investor_take=orm.investor_take,
        ai_model=orm.ai_model,
        rejected_at=orm.rejected_at,
    )


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


# ---------------------------------------------------------------------------
# 成功経路 — append_in_scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_in_scope_records_success_with_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """category=success / code=assessed_in_scope と payload の主要 field を確認。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    in_scope = await _persist_in_scope(db_session, extraction, sample_categories[0])

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            envelope=_in_scope_envelope(),
            assessment=in_scope,
            ai_model=_AI_MODEL,
            category_slug="ai",
            code="assessed_in_scope",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "assessed_in_scope"
    assert ev.category == "success"
    assert ev.code == "assessed_in_scope"
    assert ev.payload["extraction_id"] == extraction.id
    assert ev.payload["assessment_id"] == in_scope.id
    assert ev.payload["category_id"] == sample_categories[0].id
    assert ev.payload["topic"] == "llm benchmark"
    assert ev.payload["investor_take"] == "bullish"
    assert ev.payload["ai_model"] == _AI_MODEL


@pytest.mark.asyncio
async def test_append_in_scope_records_category_slug_from_caller(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """category_slug は caller 渡しで envelope.raw_category と独立して格納される。

    raw_category=AI 生値 (validation 前) / category_slug=catalog 確認後 slug の
    意味分離を test で固定する。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    in_scope = await _persist_in_scope(db_session, extraction, sample_categories[0])
    # envelope.raw_category と異なる値を caller 渡し
    envelope = _in_scope_envelope()
    envelope_with_diff_raw = AssessmentCall(
        result=envelope.result,
        raw_response=envelope.raw_response,
        raw_category="ai_raw_from_envelope",
        raw_topic=envelope.raw_topic,
        prompt_version=envelope.prompt_version,
    )

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            envelope=envelope_with_diff_raw,
            assessment=in_scope,
            ai_model=_AI_MODEL,
            category_slug="ai_catalog_confirmed",  # caller が渡す
            code="assessed_in_scope",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["raw_category"] == "ai_raw_from_envelope"  # envelope 由来
    assert ev.payload["category_slug"] == "ai_catalog_confirmed"  # caller 渡し


@pytest.mark.asyncio
async def test_append_in_scope_resolves_article_id_from_extraction(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """pipeline_events.article_id が extraction → article 逆引きで解決される。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    in_scope = await _persist_in_scope(db_session, extraction, sample_categories[0])

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            envelope=_in_scope_envelope(),
            assessment=in_scope,
            ai_model=_AI_MODEL,
            category_slug="ai",
            code="assessed_in_scope",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.article_id == article.id  # extraction → article で解決


@pytest.mark.asyncio
async def test_append_in_scope_resolves_source_name(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """payload.source_name が extraction → article → news_source の 2-hop で解決。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    in_scope = await _persist_in_scope(db_session, extraction, sample_categories[0])

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            envelope=_in_scope_envelope(),
            assessment=in_scope,
            ai_model=_AI_MODEL,
            category_slug="ai",
            code="assessed_in_scope",
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
    in_scope = await _persist_in_scope(db_session, extraction, sample_categories[0])

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            envelope=_in_scope_envelope(),
            assessment=in_scope,
            ai_model=_AI_MODEL,
            category_slug="ai",
            code="assessed_in_scope",
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
    in_scope = await _persist_in_scope(db_session, extraction, sample_categories[0])
    huge_raw = "x" * 5000
    envelope = AssessmentCall(
        result=InScope(
            category=InScopeCategory.AI,
            topic=TopicName("llm benchmark"),
            investor_take="bullish",
        ),
        raw_response=huge_raw,
        raw_category="ai",
        raw_topic="LLM Benchmark",
        prompt_version="abcd1234",
    )

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_in_scope(
            ready=_ready(extraction),
            envelope=envelope,
            assessment=in_scope,
            ai_model=_AI_MODEL,
            category_slug="ai",
            code="assessed_in_scope",
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
    """category=success / code=assessed_out_of_scope。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    out_of_scope = await _persist_out_of_scope(db_session, extraction)

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_out_of_scope(
            ready=_ready(extraction),
            envelope=_out_of_scope_envelope(),
            assessment=out_of_scope,
            ai_model=_AI_MODEL,
            code="assessed_out_of_scope",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "assessed_out_of_scope"
    assert ev.category == "success"
    assert ev.code == "assessed_out_of_scope"


@pytest.mark.asyncio
async def test_append_out_of_scope_payload_has_only_assessment_id(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """spec 状態識別表 line 962: out-of-scope は assessment_id のみ非 None。

    in-scope 系 4 field (category_id / category_slug / topic / investor_take)
    は全て None。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    out_of_scope = await _persist_out_of_scope(db_session, extraction)

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_out_of_scope(
            ready=_ready(extraction),
            envelope=_out_of_scope_envelope(),
            assessment=out_of_scope,
            ai_model=_AI_MODEL,
            code="assessed_out_of_scope",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["assessment_id"] == out_of_scope.id  # 非 None
    assert ev.payload["category_id"] is None
    assert ev.payload["category_slug"] is None
    assert ev.payload["topic"] is None
    assert ev.payload["investor_take"] is None


# ---------------------------------------------------------------------------
# 失敗経路 — append_failure (Layer 1 marker dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_failure_recoverable_maps_to_retryable(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AssessmentRecoverableError → category=retryable / code=instance attr。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = AssessmentRecoverableError("net error", code="ai_error_network")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
            attempt=1,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "failed"
    assert ev.category == "retryable"
    assert ev.code == "ai_error_network"
    assert ev.outcome_code == "ai_error_network"


@pytest.mark.asyncio
async def test_append_failure_terminal_skip_maps_to_keep_extraction(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AssessmentTerminalSkipError → category=non_retryable_keep_extraction。

    Stage 4 の意図的命名差: extraction は捨てない、article 保持の最も
    保守的な category。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = AssessmentTerminalSkipError("rejected", code="ai_error_input_rejected")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
            attempt=1,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.category == "non_retryable_keep_extraction"
    assert ev.code == "ai_error_input_rejected"


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
    exc = AssessmentResponseInvalidError("schema mismatch")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
            attempt=1,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.category == "retryable"
    assert ev.code == "assessment_response_invalid"


@pytest.mark.asyncio
async def test_append_failure_layer_2b_category_missing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Layer 2-B AssessmentCategoryMissingError は TerminalSkip 継承で
    category=non_retryable_keep_extraction にマップされる。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = AssessmentCategoryMissingError("unknown slug 'foo'")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
            attempt=1,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.category == "non_retryable_keep_extraction"
    assert ev.code == "assessment_category_missing"


@pytest.mark.asyncio
async def test_append_failure_unknown_exception_maps_to_unknown(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """非 marker exception は category=unknown / code=unexpected_error。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = RuntimeError("boom")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
            attempt=1,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.category == "unknown"
    assert ev.code == "unexpected_error"


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
            raise AssessmentRecoverableError(
                "wrapped", code="ai_error_network"
            ) from inner
    except AssessmentRecoverableError as exc:
        async with session_factory() as session:
            await AssessmentAuditRepository(session).append_failure(
                ready=_ready(extraction),
                exc=exc,
                attempt=1,
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
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
            attempt=1,
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
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
            attempt=1,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["ai_raw_response"] is not None
    assert len(ev.payload["ai_raw_response"]) == 2048


@pytest.mark.asyncio
async def test_append_failure_records_attempt(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """任意 marker / attempt=3 → pipeline_events.attempt == 3。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = AssessmentRecoverableError("net", code="ai_error_network")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
            attempt=3,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.attempt == 3


@pytest.mark.asyncio
async def test_append_failure_records_extraction_id_in_payload(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """payload.extraction_id が ready.extraction_id と一致する。

    Stage 4 固有 identifier (top-level column が無いため payload で保持)。
    """
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    exc = RuntimeError("boom")

    async with session_factory() as session:
        await AssessmentAuditRepository(session).append_failure(
            ready=_ready(extraction),
            exc=exc,
            attempt=1,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["extraction_id"] == extraction.id
