"""``BriefingAuditRepository`` の統合テスト。

- 5 semantic API 各 1 (``append_completed`` / ``append_input_empty`` /
  ``append_failure`` / ``append_dispatched`` / ``append_dispatcher_failure``)
- ``append_failure`` は failure projection parametrize +
  ``retry_exhausted`` 軸 (``True`` / ``None`` = retry 中)
- repository は ``commit`` を呼ばない (caller の tx 境界保持)
- ``error_chain`` は ``__cause__`` を辿り 2 段以上を記録
- ``error_message`` は ``redact_secrets()`` 経由
"""

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
    OUTCOME_BRIEFING_COMPLETED,
    OUTCOME_BRIEFING_DISPATCHED,
    OUTCOME_BRIEFING_INPUT_EMPTY,
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

# ===========================================================================
# Integration tests — 5 semantic API
# ===========================================================================


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
async def test_append_completed_records_succeeded_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """成功 audit が SUCCEEDED + outcome_code + payload 整合で記録される。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_completed(
            ready=_ready(ai_category.id),
            article_count=7,
            ai_model="deepseek-v4-pro",
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == OUTCOME_BRIEFING_COMPLETED
    assert ev.retryability is None
    assert ev.payload["kind"] == "briefing"
    assert ev.payload["week_start"] == "2026-04-20"
    assert ev.payload["category_id"] == ai_category.id
    assert ev.payload["category_slug"] == "ai"
    assert ev.payload["article_count"] == 7
    assert ev.payload["ai_model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_append_input_empty_records_rejected_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """記事ゼロが REJECTED + outcome_code + article_count=0 で記録される。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_input_empty(
            ready=_ready(ai_category.id),
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "rejected"
    assert ev.outcome_code == OUTCOME_BRIEFING_INPUT_EMPTY
    assert ev.retryability is None
    assert ev.payload["article_count"] == 0
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
            "briefing_configuration_error",
            "non_retryable",
            "configuration",
            None,
        ),
        (
            lambda: BriefingResponseInvalidError(),
            "briefing_response_invalid",
            "non_retryable",
            "response_invalid",
            None,
        ),
        (
            lambda: BriefingLlmError(provider_error=RuntimeError("upstream")),
            "briefing_llm_error",
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
async def test_append_failure_projects_exceptions(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
    exc_factory: object,
    expected_outcome_code: str,
    expected_retryability: str,
    expected_failure_kind: str,
    expected_failure_action: str | None,
) -> None:
    """failure audit が例外クラスから projection を導出する。"""
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
    """``retry_exhausted=True`` のみ payload に出る (``None`` 時は null/欠落)。

    ``CompletionPayload`` precedent と同型: extrinsic な give-up timing で、
    retry 上限到達時のみ記録する。consumer は
    ``payload @> '{"retry_exhausted": true}'`` で give-up を集計する。
    """
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
    """``error_message`` が ``redact_secrets()`` を通る (SDK exception の
    API key 混入経路を redact)。
    """
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
    # JWT 本体 (eyJ...) が生のまま残っていないことを構造的に検証
    assert "eyJhbGciOiJIUzI1NiJ9" not in msg


@pytest.mark.asyncio
async def test_append_dispatched_records_weekly_anchor(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """dispatcher の週次成功 anchor: per-category 軸は埋めず category_count のみ。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_dispatched(
            week_start=date(2026, 4, 20),
            category_count=3,
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == OUTCOME_BRIEFING_DISPATCHED
    assert ev.retryability is None
    assert ev.payload["week_start"] == "2026-04-20"
    assert ev.payload["category_count"] == 3
    # per-category 軸は埋めない
    assert ev.payload["category_id"] is None
    assert ev.payload["category_slug"] is None
    assert ev.payload["article_count"] is None


@pytest.mark.asyncio
async def test_append_dispatcher_failure_marks_retry_exhausted_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """dispatcher 失敗 anchor: ``max_retries=0`` 即 give-up を
    ``retry_exhausted=True`` で示す。
    """
    exc = RuntimeError("session_factory misconfigured")
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_unexpected_dispatcher_failure(
            week_start=date(2026, 4, 20),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "failed"
    assert ev.outcome_code == "unexpected_error"
    assert ev.payload["retry_exhausted"] is True
    assert ev.payload["week_start"] == "2026-04-20"
    assert ev.retryability == "unknown"
    assert ev.payload["failure_kind"] == "unknown"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_repository_does_not_commit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """repository は ``session.commit()`` を呼ばない (caller の tx 境界保持)。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_completed(
            ready=_ready(ai_category.id),
            article_count=1,
            ai_model="deepseek-v4-pro",
        )
        # 意図的に commit しない (session close = rollback)

    rows = (await db_session.execute(select(PipelineEvent))).scalars().all()
    assert len(rows) == 0
