"""``CurationFailureHandler`` の integration test。

検証する性質 (Drop 経路):
- 1 tx 内で audit INSERT → article DELETE が両方完了する
- 順序: audit が先 (source_id 自動補完が article 健在時に確定)
- DELETE 後、``articles`` から該当 row が消える
- ``pipeline_events.article_id`` は ``ondelete=SET NULL`` のため audit 行は残る
  ただし新規 INSERT 時点では ``article_id`` が埋まっている (DELETE 前)
- ``source_id`` が auto-resolve される (article DELETE 後でも source 追跡可能)
- ``CurationTerminalDropError`` (ACL ``map_provider_to_curation`` で
  ``AIProviderOutputBlockedError`` / ``AIProviderInputRejectedError`` から
  詰め替えられる) で ``outcome_code`` / ``retryability`` /
  payload failure attrs が記録される
- 戻り値 ``False`` (Drop 経路は taskiq retry させない)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.gemini_spec import GEMINI_CURATION_SPEC
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import map_provider_to_curation
from app.analysis.curation.failure_handling import CurationFailureHandler
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _curator_mock() -> MagicMock:
    """Handler に渡す ``BaseCurator`` mock (model_name / prompt_version のみ)。"""
    mock = MagicMock(spec=BaseCurator)
    type(mock).model_name = GEMINI_CURATION_SPEC.model
    type(mock).prompt_version = GEMINI_CURATION_SPEC.version
    return mock


async def _make_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/a",
    content: str = "body content " * 30,
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="t",
        original_content=content,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


def _ready_from(article: Article) -> ReadyForCuration:
    return ReadyForCuration(
        article_id=article.id,
        original_title=article.original_title,
        original_content=article.original_content,
    )


@pytest.mark.asyncio
async def test_output_blocked_writes_audit_then_deletes_article(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AIProviderOutputBlockedError 経路で failure attrs が正しく記録される。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    # rollback 後の expired-attr lazy reload を避けるため事前に値を取り出す
    expected_source_id = sample_source.id
    expected_raw_length = len(article.original_content)
    ready = _ready_from(article)
    handler = CurationFailureHandler(session_factory)

    raw_exc = AIProviderOutputBlockedError("blocked by policy: SAFETY")
    try:
        raise map_provider_to_curation(raw_exc) from raw_exc
    except Exception as wrapped:  # noqa: BLE001
        exc = wrapped
    decision = await handler.handle(
        ready=ready,
        exc=exc,
        curator=_curator_mock(),
        last_attempt=False,
    )

    assert decision.reraise is False
    assert decision.stage_hold_reason is None

    # commit が走った別 tx の DB 状態を確認するため fresh session で検証
    await db_session.rollback()
    article_row = await db_session.get(Article, article_id)
    assert article_row is None  # DELETE 済

    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "curation")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_output_blocked"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_drop"
    assert ev.payload["failure_action"] == "drop_article"
    # SET NULL: article_id は NULL に
    assert ev.article_id is None
    # source_id は auto-resolve で埋まっている (DELETE 前に INSERT したため)
    assert ev.source_id == expected_source_id
    # 記事識別子は削除に耐える payload snapshot で残る (article_id は SET NULL)
    assert ev.payload["target_article_id"] == article_id
    payload = ev.payload
    assert payload["ai_model"] == GEMINI_CURATION_SPEC.model
    assert payload["error_message"] is not None
    assert payload["error_chain"] is not None
    # audit repository が drop 経路でも original_content から値を焼く。
    assert payload["input_content_length"] == expected_raw_length
    assert payload["input_content_head"]  # non-empty
    assert len(payload["input_content_hash"]) == 16


@pytest.mark.asyncio
async def test_input_rejected_writes_audit_then_deletes_article(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AIProviderInputRejectedError 経路 (context length 超過 etc) も同様に記録。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_from(article)
    handler = CurationFailureHandler(session_factory)

    raw_exc = AIProviderInputRejectedError("input exceeds context length")
    try:
        raise map_provider_to_curation(raw_exc) from raw_exc
    except Exception as wrapped:  # noqa: BLE001
        exc = wrapped
    decision = await handler.handle(
        ready=ready,
        exc=exc,
        curator=_curator_mock(),
        last_attempt=False,
    )

    assert decision.reraise is False
    assert decision.stage_hold_reason is None
    await db_session.rollback()
    assert (await db_session.get(Article, article_id)) is None
    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "curation")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_input_rejected"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_drop"
    assert ev.payload["failure_action"] == "drop_article"


# hold gate — terminal_keep は failure 観測時に curation hold を立てる


def _wrap(raw: BaseException) -> BaseException:
    """ACL で Stage 3 marker に詰め替え + ``__cause__`` を保持する helper。"""
    try:
        raise map_provider_to_curation(raw) from raw  # type: ignore[arg-type]
    except BaseException as wrapped:  # noqa: BLE001
        return wrapped


@pytest.mark.asyncio
async def test_terminal_keep_sets_curation_hold(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """terminal_keep は failure audit を焼いた上で hold を失敗 code で立てる。"""
    article = await _make_article(db_session, sample_source)
    # rollback 後の expired-attr lazy reload を避けるため事前に id を取り出す
    article_id = article.id
    expected_raw_length = len(article.original_content)
    ready = _ready_from(article)
    handler = CurationFailureHandler(session_factory)
    # AIProviderConfigurationError → CurationTerminalKeepError (keep, config 起因)
    exc = _wrap(AIProviderConfigurationError("api key missing"))

    decision = await handler.handle(
        ready=ready,
        exc=exc,
        curator=_curator_mock(),
        last_attempt=False,
    )

    assert decision.reraise is False  # terminal_keep は taskiq retry させない
    # reason は失敗 code (exc.code 由来、provider 健全性問題の識別子)
    assert decision.stage_hold_reason == exc.code
    # failure audit は hold と独立に必ず残す
    await db_session.rollback()
    ev = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article_id)
            )
        )
        .scalars()
        .one()
    )
    assert ev.outcome_code == "ai_error_configuration"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_keep"
    assert ev.payload["failure_action"] is None
    # audit repository が failure 経路でも original_content から値を焼く。
    assert ev.payload["input_content_length"] == expected_raw_length
    assert ev.payload["input_content_head"]  # non-empty
    assert len(ev.payload["input_content_hash"]) == 16


@pytest.mark.asyncio
async def test_recoverable_does_not_set_curation_hold(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """recoverable 失敗では hold を立てない (hold は failure の性質で立つ)。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_from(article)
    handler = CurationFailureHandler(session_factory)
    # AIProviderNetworkError → CurationRecoverableError (retryable)
    exc = _wrap(AIProviderNetworkError("conn reset"))

    decision = await handler.handle(
        ready=ready,
        exc=exc,
        curator=_curator_mock(),
        last_attempt=True,
    )

    assert decision.reraise is False
    assert decision.stage_hold_reason is None


@pytest.mark.asyncio
async def test_usage_limit_recoverable_sets_curation_hold_on_last_attempt(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """UsageLimitExhausted は recoverable のまま retry exhaustion で hold する。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_from(article)
    handler = CurationFailureHandler(session_factory)
    exc = _wrap(AIProviderUsageLimitExhaustedError("usage exhausted"))

    decision = await handler.handle(
        ready=ready,
        exc=exc,
        curator=_curator_mock(),
        last_attempt=True,
    )

    assert decision.reraise is False
    assert decision.stage_hold_reason == exc.code
    await db_session.rollback()
    ev = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article_id)
            )
        )
        .scalars()
        .one()
    )
    assert ev.outcome_code == AIProviderUsageLimitExhaustedError.CODE
    assert ev.retryability == "retryable"
    assert ev.payload["failure_kind"] == "recoverable"


@pytest.mark.asyncio
async def test_usage_limit_recoverable_with_retry_budget_does_not_set_hold(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """UsageLimitExhausted でも retry 余地があれば taskiq retry に任せる。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_from(article)
    handler = CurationFailureHandler(session_factory)
    exc = _wrap(AIProviderUsageLimitExhaustedError("usage exhausted"))

    decision = await handler.handle(
        ready=ready,
        exc=exc,
        curator=_curator_mock(),
        last_attempt=False,
    )

    assert decision.reraise is True
    assert decision.stage_hold_reason is None


@pytest.mark.asyncio
async def test_rate_limited_recoverable_last_attempt_does_not_set_curation_hold(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """RateLimited は短期 throttle として recoverable exhaustion でも hold しない。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_from(article)
    handler = CurationFailureHandler(session_factory)
    exc = _wrap(AIProviderRateLimitedError("429"))

    decision = await handler.handle(
        ready=ready,
        exc=exc,
        curator=_curator_mock(),
        last_attempt=True,
    )

    assert decision.reraise is False
    assert decision.stage_hold_reason is None
