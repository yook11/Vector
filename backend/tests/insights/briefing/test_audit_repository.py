"""``BriefingAuditRepository`` の単体 + 統合テスト。

unit セクション:
- ``_code_of`` / ``_category_of`` の例外クラス → ラベル写像 (pure 関数)
  - ``BriefingConfigurationError`` / pydantic ``ValidationError`` / ``openai.APIError``
    / SQLAlchemy 4 兄弟 / catch-all
  - retry-status 駆動 (D8 撤回) ではなく **exception-class 駆動** であることを
    各クラスの fixed mapping で検証する

integration セクション:
- 5 semantic API 各 1 (``append_completed`` / ``append_input_empty`` /
  ``append_failure`` / ``append_dispatched`` / ``append_dispatcher_failure``)
- ``append_failure`` は (exc, expected_category, expected_code) parametrize +
  ``retry_exhausted`` 軸 (``True`` / ``None`` = 中間 attempt)
- repository は ``commit`` を呼ばない (caller の tx 境界保持)
- ``error_chain`` は ``__cause__`` を辿り 2 段以上を記録
- ``error_message`` は ``redact_secrets()`` 経由
"""

from __future__ import annotations

from datetime import date

import httpx
import openai
import pytest
from openai import APIError as OpenAIAPIError
from openai import RateLimitError as OpenAIRateLimitError
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.categories import Layer1Category
from app.insights.briefing.audit_repository import (
    OUTCOME_BRIEFING_COMPLETED,
    OUTCOME_BRIEFING_DISPATCHED,
    OUTCOME_BRIEFING_INPUT_EMPTY,
    BriefingAuditRepository,
)
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.llm.errors import BriefingConfigurationError
from app.models.category import Category
from app.models.pipeline_event import PipelineEvent

# ===========================================================================
# Unit tests — _code_of / _category_of (pure function, no DB)
# ===========================================================================


def _make_validation_error() -> ValidationError:
    """pydantic ``ValidationError`` を最小構成で生成する。"""

    class _M(BaseModel):
        x: int

    try:
        _M.model_validate({"x": "not-an-int"})
    except ValidationError as exc:
        return exc
    raise AssertionError("ValidationError was not raised")  # pragma: no cover


def _make_openai_api_error() -> OpenAIAPIError:
    """``openai.APIError`` を最小構成で生成する (SDK 基底)。"""
    request = httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")
    return OpenAIAPIError("upstream", request=request, body=None)


def _make_openai_rate_limit_error() -> OpenAIRateLimitError:
    """``openai.RateLimitError`` (``APIError`` の派生) を最小構成で生成する。"""
    request = httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")
    response = httpx.Response(429, request=request)
    return OpenAIRateLimitError("rate limit", response=response, body=None)


@pytest.mark.parametrize(
    ("exc_factory", "expected_code"),
    [
        # 自前 marker (BriefingConfigurationError)
        (
            lambda: BriefingConfigurationError("API key missing"),
            "briefing_configuration_error",
        ),
        # pydantic ValidationError (schema バグ / ハルシネーション)
        (_make_validation_error, "briefing_response_invalid"),
        # openai.APIError 基底
        (_make_openai_api_error, "briefing_llm_error"),
        # openai.RateLimitError は APIError の派生なので同じ
        (_make_openai_rate_limit_error, "briefing_llm_error"),
        # SQLAlchemy: 接続断 / deadlock
        (
            lambda: OperationalError("SELECT 1", {}, Exception("conn reset")),
            "db_runtime_error",
        ),
        # SQLAlchemy: unique / FK 違反
        (
            lambda: IntegrityError("INSERT", {}, Exception("unique violation")),
            "db_constraint_error",
        ),
        # SQLAlchemy: SQL / カラム不在
        (
            lambda: ProgrammingError("SELECT bad", {}, Exception("no such column")),
            "db_query_or_schema_error",
        ),
        # SQLAlchemy: 未分類 SQLAlchemyError 直系
        (
            lambda: InvalidRequestError("detached instance"),
            "db_unknown_error",
        ),
        # catch-all (knwon marker でも DB 例外でも openai でもない)
        (lambda: RuntimeError("boom"), "unexpected_error"),
    ],
)
def test_code_of_maps_exception_to_code(
    exc_factory: object, expected_code: str
) -> None:
    """``_code_of`` は例外クラスから wire 値 ``code`` を導出する。

    期待値は spec の D4 マッピング表から決め、production 関数で再生成しない
    (``feedback_test_first_discovery_not_confirmatory``)。
    """
    exc = exc_factory()  # type: ignore[operator]
    assert BriefingAuditRepository._code_of(exc) == expected_code


@pytest.mark.parametrize(
    ("exc_factory", "expected_category"),
    [
        # 自前 marker: intrinsic に retry で直らない (API key 欠落等)
        (
            lambda: BriefingConfigurationError("API key missing"),
            Layer1Category.NON_RETRYABLE,
        ),
        # pydantic ValidationError: schema バグ、保守的に non_retryable
        (_make_validation_error, Layer1Category.NON_RETRYABLE),
        # openai.APIError: transient (RateLimit / 5xx / network)
        (_make_openai_api_error, Layer1Category.RETRYABLE),
        # openai.RateLimitError も派生で retryable
        (_make_openai_rate_limit_error, Layer1Category.RETRYABLE),
        # SQLAlchemy RUNTIME: retry-friendly
        (
            lambda: OperationalError("SELECT 1", {}, Exception("conn reset")),
            Layer1Category.RETRYABLE,
        ),
        # SQLAlchemy CONSTRAINT: retry で直らない (制約違反)
        (
            lambda: IntegrityError("INSERT", {}, Exception("unique violation")),
            Layer1Category.NON_RETRYABLE,
        ),
        # SQLAlchemy QUERY_OR_SCHEMA: retry で直らない (SQL バグ)
        (
            lambda: ProgrammingError("SELECT bad", {}, Exception("no such column")),
            Layer1Category.NON_RETRYABLE,
        ),
        # SQLAlchemy 未分類: UNKNOWN
        (lambda: InvalidRequestError("detached instance"), Layer1Category.UNKNOWN),
        # catch-all
        (lambda: RuntimeError("boom"), Layer1Category.UNKNOWN),
    ],
)
def test_category_of_is_exception_class_driven(
    exc_factory: object, expected_category: Layer1Category
) -> None:
    """``_category_of`` は exception class で intrinsic な retry-friendliness を決める。

    retry-status (attempt 番号) には依存しない。retry 上限到達は payload
    ``retry_exhausted`` で表現する別軸 (D8 改訂版)。
    """
    exc = exc_factory()  # type: ignore[operator]
    assert BriefingAuditRepository._category_of(exc) == expected_category


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
    """成功 audit が SUCCEEDED + category=success + payload 整合で記録される。"""
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
    assert ev.category == "success"
    assert ev.code == OUTCOME_BRIEFING_COMPLETED
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
    """記事ゼロが REJECTED + category=NULL + article_count=0 で記録される。"""
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_input_empty(
            ready=_ready(ai_category.id),
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "rejected"
    assert ev.outcome_code == OUTCOME_BRIEFING_INPUT_EMPTY
    assert ev.category is None  # retry 概念外、event_type で完結
    assert ev.code == OUTCOME_BRIEFING_INPUT_EMPTY
    assert ev.payload["article_count"] == 0
    assert ev.payload["category_slug"] == "ai"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_factory", "expected_category", "expected_code"),
    [
        (
            lambda: BriefingConfigurationError("DEEPSEEK_API_KEY missing"),
            "non_retryable",
            "briefing_configuration_error",
        ),
        (_make_validation_error, "non_retryable", "briefing_response_invalid"),
        (_make_openai_api_error, "retryable", "briefing_llm_error"),
        (
            lambda: OperationalError("SELECT 1", {}, Exception("conn reset")),
            "retryable",
            "db_runtime_error",
        ),
        (
            lambda: IntegrityError("INSERT", {}, Exception("unique violation")),
            "non_retryable",
            "db_constraint_error",
        ),
        (lambda: RuntimeError("boom"), "unknown", "unexpected_error"),
    ],
)
async def test_append_failure_classifies_exceptions(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
    exc_factory: object,
    expected_category: str,
    expected_code: str,
) -> None:
    """failure audit が例外クラスから category / code を導出する。

    ``outcome_code = code`` (Phase A 不変)、``error_class`` は exc の FQN。
    """
    exc = exc_factory()  # type: ignore[operator]
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_failure(
            ready=_ready(ai_category.id),
            exc=exc,
            attempt=2,
            retry_exhausted=None,
            ai_model="deepseek-v4-pro",
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.event_type == "failed"
    assert ev.category == expected_category
    assert ev.code == expected_code
    assert ev.outcome_code == expected_code  # Phase A 不変
    assert ev.attempt == 2
    assert ev.error_class is not None
    assert ev.error_class.endswith(type(exc).__qualname__)
    assert ev.payload["ai_model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_append_failure_records_retry_exhausted_only_when_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    ai_category: Category,
) -> None:
    """``retry_exhausted=True`` のみ payload に出る (``None`` 時は null/欠落)。

    ``CompletionPayload`` precedent と同型: extrinsic な give-up timing で、
    最終 attempt のみ記録する。consumer は
    ``payload @> '{"retry_exhausted": true}'`` で give-up を集計する。
    """
    async with session_factory() as session:
        await BriefingAuditRepository(session).append_failure(
            ready=_ready(ai_category.id),
            exc=RuntimeError("last attempt boom"),
            attempt=3,
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
                attempt=1,
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
        await BriefingAuditRepository(session).append_failure(
            ready=_ready(ai_category.id),
            exc=exc,
            attempt=1,
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
    assert ev.category == "success"
    assert ev.code == OUTCOME_BRIEFING_DISPATCHED
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
        await BriefingAuditRepository(session).append_dispatcher_failure(
            week_start=date(2026, 4, 20),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session)
    assert ev.stage == "briefing"
    assert ev.event_type == "failed"
    assert ev.payload["retry_exhausted"] is True
    assert ev.payload["week_start"] == "2026-04-20"
    assert ev.category == "unknown"  # RuntimeError → unknown
    assert ev.code == "unexpected_error"


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


# ===========================================================================
# openai import sanity — _category_of の openai 分岐が再帰的に動くこと
# (HTTP status 系派生クラスも APIError 基底経由で retryable に分類されることを担保)
# ===========================================================================


def test_openai_status_subclasses_inherit_apierror() -> None:
    """``_category_of`` の ``isinstance(exc, openai.APIError)`` 分岐が SDK 派生 (5xx /
    429) も RETRYABLE に振る前提を構造的に保証する pin。SDK 階層が変わったら気付く。
    """
    assert issubclass(openai.RateLimitError, openai.APIError)
    assert issubclass(openai.APIStatusError, openai.APIError)
    assert issubclass(openai.APIConnectionError, openai.APIError)
