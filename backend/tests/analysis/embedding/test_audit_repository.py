"""``EmbeddingAuditRepository`` の semantic method 単独テスト。

audit row の shape SSoT が repository に集約されたことを検証する:

- ``append_success`` で
  ``outcome_code="embedding_completed"`` + payload に
  ``embedding_model`` / ``vector_dimension`` が embedder から取得されている
- ``append_failure`` で **exc 型による 2 dispatch + Layer 2-B + catch-all** が動作:
  - ``EmbeddingRecoverableError`` → ``retryability=retryable``
  - ``EmbeddingTerminalStageBlockedError`` → ``retryability=non_retryable``
  - ``EmbeddingResponseInvalidError`` (Layer 2-B) → ``retryability=retryable`` /
    ``outcome_code="embedding_response_invalid"``
  - 想定外 ``RuntimeError`` → ``retryability=unknown`` /
    ``outcome_code="unexpected_error"``
- ``error_chain`` が ``__cause__`` 経由で 2 段以上を記録 (Service の
  ``raise to_embedding_error(exc) from exc`` の wrapper 連鎖を想定)
- ``error_message`` が ``redact_secrets()`` 経由
- repository は ``commit`` を呼ばない (caller の tx 境界保持)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import (
    EmbeddingRecoverableError,
    EmbeddingResponseInvalidError,
    EmbeddingTerminalStageBlockedError,
    EmbeddingTerminalTargetRejectedError,
)
from app.audit.stages.embedding import EmbeddingAuditRepository
from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _embedder_fake(
    *, model: str = "cl-nagoya/ruri-v3-310m", dimension: int = 768
) -> MagicMock:
    """audit に焼く model_name / dimension property を持つ embedder スタブ。"""
    fake = MagicMock(spec=BaseEmbedder)
    fake.model_name = model
    fake.dimension = dimension
    return fake


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
) -> ArticleCuration:
    extraction = ArticleCuration(
        article_id=article.id,
        translated_title="title",
        summary="summary",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


def _ready(article: Article) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=1,
        text_for_embedding="title\nsummary",
        article_id=article.id,
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
# 成功経路 — append_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_success_records_with_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """succeeded / outcome_code=embedding_completed + payload に model / dimension。"""
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_success(
            ready=_ready(article),
            embedder=_embedder_fake(),
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "embedding_completed"
    assert ev.retryability is None
    assert ev.payload["embedding_model"] == "cl-nagoya/ruri-v3-310m"
    assert ev.payload["vector_dimension"] == 768


@pytest.mark.asyncio
async def test_append_success_uses_article_id_from_ready(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """pipeline_events.article_id が Ready 由来 (案 3: Ready 構築時に取得済)。"""
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_success(
            ready=_ready(article),
            embedder=_embedder_fake(),
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.article_id == article.id  # ready.article_id を直接利用


@pytest.mark.asyncio
async def test_append_success_does_not_commit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """repository は session.commit() を呼ばない (caller tx 境界保持)。"""
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_success(
            ready=_ready(article),
            embedder=_embedder_fake(),
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


# ---------------------------------------------------------------------------
# 失敗経路 — append_failure (Layer 1 marker dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_failure_recoverable_maps_to_retryable(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """EmbeddingRecoverableError → retryable / outcome_code=instance attr。"""
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)
    exc = EmbeddingRecoverableError(code="ai_error_network")

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_failure(
            ready=_ready(article),
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
    """EmbeddingTerminalStageBlockedError → non_retryable / stage_blocked。"""
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)
    exc = EmbeddingTerminalStageBlockedError(code="ai_error_configuration")

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_failure(
            ready=_ready(article),
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
    """EmbeddingTerminalTargetRejectedError → non_retryable / target_rejected。"""
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)
    exc = EmbeddingTerminalTargetRejectedError(code="ai_error_input_rejected")

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_failure(
            ready=_ready(article),
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
    """Layer 2-B EmbeddingResponseInvalidError は Recoverable 継承で retryable。

    ctor は message のみ、code は内部で hardcode (embedding_response_invalid)。
    """
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)
    exc = EmbeddingResponseInvalidError()

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_failure(
            ready=_ready(article),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == "embedding_response_invalid"
    assert ev.retryability == "retryable"
    assert ev.payload["failure_kind"] == "recoverable"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_append_failure_unknown_exception_maps_to_unknown(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """非 marker exception は unknown / outcome_code=unexpected_error。"""
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)
    exc = RuntimeError("boom")

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_unexpected_failure(
            ready=_ready(article),
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
    await _make_extraction(db_session, article)
    exc = exc_factory()  # type: ignore[operator]

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_failure(
            ready=_ready(article),
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
    """error_chain は __cause__ を辿り 2 段以上を記録する。

    Service の ``raise to_embedding_error(exc) from exc`` で
    Layer 1 marker (wrapper) と元 ``AIProvider*Error`` の両方が必要。
    """
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)
    try:
        try:
            raise RuntimeError("upstream provider error")
        except RuntimeError as inner:
            # Phase 4: kwargs-only constructor。message 引数廃止 (PII 隔離契約)。
            raise EmbeddingRecoverableError(code="ai_error_network") from inner
    except EmbeddingRecoverableError as exc:
        async with session_factory() as session:
            await EmbeddingAuditRepository(session).append_failure(
                ready=_ready(article),
                exc=exc,
            )
            await session.commit()

    ev = await _fetch_one(db_session, article.id)
    chain = ev.payload["error_chain"]
    assert chain is not None
    assert len(chain) >= 2
    assert chain[0].endswith(".EmbeddingRecoverableError")
    assert chain[1].endswith(".RuntimeError")


@pytest.mark.asyncio
async def test_append_failure_redacts_secrets_in_error_message(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """error_message が redact_secrets() 経由で永続化される (red-team chain γ-2)。"""
    article = await _make_article(db_session, sample_source)
    await _make_extraction(db_session, article)
    exc = RuntimeError(
        "Authorization: Bearer "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2QT4abc failed"
    )

    async with session_factory() as session:
        await EmbeddingAuditRepository(session).append_unexpected_failure(
            ready=_ready(article),
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.payload["error_message"] is not None
    assert "SflKxwRJSMeKKF2QT4abc" not in ev.payload["error_message"]
    assert "***" in ev.payload["error_message"]
