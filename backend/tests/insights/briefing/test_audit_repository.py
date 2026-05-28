"""``BriefingAuditRepository`` の統合テスト。"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import (
    IntegrityError,
    OperationalError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.stages.briefing import (
    OUTCOME_BRIEFING_CATEGORY_ENQUEUE_FAILED,
    OUTCOME_BRIEFING_CATEGORY_ENQUEUED,
    OUTCOME_BRIEFING_DISPATCH_CATEGORY_MASTER_LOAD_FAILED,
    OUTCOME_BRIEFING_DISPATCH_COMPLETED,
    OUTCOME_BRIEFING_GENERATION_ALREADY_EXISTS,
    OUTCOME_BRIEFING_GENERATION_COMPLETED,
    OUTCOME_BRIEFING_GENERATION_INPUT_EMPTY,
    BriefingAuditRepository,
)
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.llm.errors import (
    BriefingConfigurationError,
    BriefingLlmError,
    BriefingResponseInvalidError,
)
from app.models.category import Category
from app.models.pipeline_event import PipelineEvent


@pytest.fixture
async def ai_category(db_session: AsyncSession) -> Category:
    cat = Category(slug="ai", name="AI")
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)
    return cat


def _ready(category_id: int, *, week: date = date(2026, 4, 20)) -> ReadyForBriefing:
    return ReadyForBriefing(week_start=week, category_id=category_id)


async def _fetch_one(db_session: AsyncSession) -> PipelineEvent:
    rows = (await db_session.execute(select(PipelineEvent))).scalars().all()
    assert len(rows) == 1, f"expected 1 event row, got {len(rows)}"
    return rows[0]


@pytest.mark.asyncio
async def test_append_generation_completed_records_succeeded_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """生成成功 audit が SUCCEEDED + outcome_code + payload 整合で記録される。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_generation_completed(
            ready=_ready(ai_category.id),
            article_count=7,
            ai_model="deepseek-v4-pro",
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == OUTCOME_BRIEFING_GENERATION_COMPLETED
    assert ev.retryability is None
    assert ev.payload["kind"] == "briefing"
    assert ev.payload["week_start"] == "2026-04-20"
    assert ev.payload["category_id"] == ai_category.id
    assert ev.payload["category_slug"] == "ai"
    assert ev.payload["article_count"] == 7
    assert ev.payload["ai_model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_append_generation_input_empty_records_rejected_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """記事ゼロが REJECTED + article_count=0 で記録される。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_generation_input_empty(
            ready=_ready(ai_category.id),
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "rejected"
    assert ev.outcome_code == OUTCOME_BRIEFING_GENERATION_INPUT_EMPTY
    assert ev.retryability is None
    assert ev.payload["article_count"] == 0
    assert ev.payload["category_slug"] == "ai"


@pytest.mark.asyncio
async def test_append_generation_already_exists_records_skipped_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """既存 briefing skip が SKIPPED として記録される。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_generation_already_exists(
            week_start=date(2026, 4, 20),
            category_id=ai_category.id,
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "skipped"
    assert ev.outcome_code == OUTCOME_BRIEFING_GENERATION_ALREADY_EXISTS
    assert ev.retryability is None
    assert ev.payload["week_start"] == "2026-04-20"
    assert ev.payload["category_id"] == ai_category.id
    assert ev.payload["category_slug"] == "ai"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "exc_factory",
        "expected_outcome_code",
        "expected_retryability",
        "expected_failure_kind",
        "expected_failure_action",
    ),
    [
        (
            lambda: BriefingConfigurationError("DEEPSEEK_API_KEY missing"),
            "briefing_generation_llm_configuration_invalid",
            "non_retryable",
            "configuration",
            None,
        ),
        (
            lambda: BriefingResponseInvalidError(),
            "briefing_generation_llm_response_contract_invalid",
            "non_retryable",
            "response_invalid",
            None,
        ),
        (
            lambda: BriefingLlmError(provider_error=RuntimeError("upstream")),
            "briefing_generation_llm_provider_call_failed",
            "retryable",
            "llm_error",
            None,
        ),
        (
            lambda: OperationalError("SELECT 1", {}, Exception("conn reset")),
            "db_runtime_error",
            "retryable",
            "db_runtime",
            None,
        ),
        (
            lambda: IntegrityError("INSERT", {}, Exception("unique violation")),
            "db_constraint_error",
            "non_retryable",
            "db_constraint",
            None,
        ),
        (
            lambda: RuntimeError("boom"),
            "unexpected_error",
            "unknown",
            "unknown",
            None,
        ),
    ],
)
async def test_append_failure_projects_generation_exceptions(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
    exc_factory: object,
    expected_outcome_code: str,
    expected_retryability: str,
    expected_failure_kind: str,
    expected_failure_action: str | None,
) -> None:
    """generation failure audit が例外クラスから projection を導出する。"""
    exc = exc_factory()  # type: ignore[operator]
    async with session_factory() as session:
        repo = BriefingAuditRepository(session)
        if isinstance(exc, RuntimeError):
            await repo.append_unexpected_failure(
                ready=_ready(ai_category.id),
                exc=exc,
                retry_exhausted=None,
                ai_model="deepseek-v4-pro",
            )
        else:
            await repo.append_failure(
                ready=_ready(ai_category.id),
                exc=exc,
                retry_exhausted=None,
                ai_model="deepseek-v4-pro",
            )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.event_type == "failed"
    assert ev.outcome_code == expected_outcome_code
    assert ev.retryability == expected_retryability
    assert ev.error_class is not None
    assert ev.error_class.endswith(type(exc).__qualname__)
    assert ev.payload["failure_kind"] == expected_failure_kind
    assert ev.payload["failure_action"] == expected_failure_action
    assert ev.payload["ai_model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_append_failure_records_retry_exhausted_only_when_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """``retry_exhausted=True`` のみ payload に出る。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_unexpected_failure(
            ready=_ready(ai_category.id),
            exc=RuntimeError("last retry boom"),
            retry_exhausted=True,
            ai_model="deepseek-v4-pro",
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.payload["retry_exhausted"] is True


@pytest.mark.asyncio
async def test_append_failure_walks_error_chain_via_cause(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """``error_chain`` が ``__cause__`` 経由で 2 段以上を記録する。"""
    try:
        try:
            raise RuntimeError("upstream LLM 5xx")
        except RuntimeError as inner:
            raise BriefingConfigurationError("wrapper") from inner
    except BriefingConfigurationError as exc:
        async with session_factory() as session:
            await BriefingAuditRepository(session).append_failure(
                ready=_ready(ai_category.id),
                exc=exc,
                retry_exhausted=None,
                ai_model="deepseek-v4-pro",
            )
            await session.commit()

    ev = await _fetch_one(db_session)
    chain = ev.payload["error_chain"]
    assert chain is not None
    assert len(chain) >= 2
    assert chain[0].endswith(".BriefingConfigurationError")
    assert chain[1].endswith(".RuntimeError")


@pytest.mark.asyncio
async def test_append_failure_redacts_secrets_in_error_message(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """``error_message`` が ``redact_secrets()`` を通る。"""
    exc = RuntimeError(
        "Authorization: Bearer "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2QT4abc failed"
    )
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_unexpected_failure(
            ready=_ready(ai_category.id),
            exc=exc,
            retry_exhausted=None,
            ai_model="deepseek-v4-pro",
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    msg = ev.payload["error_message"]
    assert msg is not None
    assert "eyJhbGciOiJIUzI1NiJ9" not in msg


@pytest.mark.asyncio
async def test_append_dispatch_completed_records_counts(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """dispatcher summary はカテゴリ count snapshot を保存する。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_dispatch_completed(
            week_start=date(2026, 4, 20),
            selected_category_count=3,
            enqueued_category_count=2,
            failed_category_count=1,
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == OUTCOME_BRIEFING_DISPATCH_COMPLETED
    assert ev.retryability is None
    assert ev.payload["week_start"] == "2026-04-20"
    assert ev.payload["selected_category_count"] == 3
    assert ev.payload["enqueued_category_count"] == 2
    assert ev.payload["failed_category_count"] == 1
    assert ev.payload["category_count"] is None
    assert ev.payload["category_id"] is None


@pytest.mark.asyncio
async def test_append_category_enqueued_records_category_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """カテゴリ単位 enqueue 成功が記録される。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_category_enqueued(
            week_start=date(2026, 4, 20),
            category_id=ai_category.id,
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == OUTCOME_BRIEFING_CATEGORY_ENQUEUED
    assert ev.retryability is None
    assert ev.payload["week_start"] == "2026-04-20"
    assert ev.payload["category_id"] == ai_category.id
    assert ev.payload["category_slug"] == "ai"


@pytest.mark.asyncio
async def test_append_category_enqueue_failed_records_error_fields(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """カテゴリ単位 enqueue 失敗は固定 outcome と error fields を保存する。"""
    exc = RuntimeError("broker unavailable")
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_category_enqueue_failed(
            week_start=date(2026, 4, 20),
            category_id=ai_category.id,
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "failed"
    assert ev.outcome_code == OUTCOME_BRIEFING_CATEGORY_ENQUEUE_FAILED
    assert ev.retryability == "unknown"
    assert ev.error_class == "builtins.RuntimeError"
    assert ev.payload["failure_kind"] == "unknown"
    assert ev.payload["category_id"] == ai_category.id
    assert ev.payload["category_slug"] == "ai"
    assert ev.payload["error_message"] == "broker unavailable"
    assert ev.payload["error_chain"] == ["builtins.RuntimeError"]


@pytest.mark.asyncio
async def test_append_dispatch_category_master_load_failed_marks_retry_exhausted_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """カテゴリマスタ取得失敗は dispatch 固定 outcome で記録される。"""
    exc = RuntimeError("session_factory misconfigured")
    async with session_factory() as session:
        await BriefingAuditRepository(
            session
        ).append_dispatch_category_master_load_failed(
            week_start=date(2026, 4, 20),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "failed"
    assert ev.outcome_code == OUTCOME_BRIEFING_DISPATCH_CATEGORY_MASTER_LOAD_FAILED
    assert ev.payload["retry_exhausted"] is True
    assert ev.payload["week_start"] == "2026-04-20"
    assert ev.retryability == "unknown"
    assert ev.payload["failure_kind"] == "unknown"
    assert ev.payload["failure_action"] is None
    assert ev.payload["error_message"] == "session_factory misconfigured"


@pytest.mark.asyncio
async def test_repository_does_not_commit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """repository は ``session.commit()`` を呼ばない。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_generation_completed(
            ready=_ready(ai_category.id),
            article_count=1,
            ai_model="deepseek-v4-pro",
        )

    rows = (await db_session.execute(select(PipelineEvent))).scalars().all()
    assert len(rows) == 0
