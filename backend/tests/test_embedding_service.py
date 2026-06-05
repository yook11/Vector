"""EmbeddingService の DB 統合テスト。

正常系の永続化/audit、並行書き込み時の短絡、AIProviderError の ACL 変換、
``EmbeddingResponseInvalidError`` の透過を確認する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import (
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderServiceUnavailableError,
)
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.errors import (
    EmbeddingRecoverableError,
    EmbeddingResponseInvalidError,
    EmbeddingTerminalTargetRejectedError,
)
from app.analysis.embedding.service import EmbeddingService
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent

# Helpers


def _mock_embedder(
    *,
    vector: EmbeddingVector | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    """固定 VO を返す (または例外を投げる) モック embedder。

    ``embed_document`` は永続化可能性を型レベルで保証する ``EmbeddingVector``
    を返す契約。Service テストでは valid VO か、境界から出る例外を直接 mock する。
    """
    embedder = MagicMock(spec=BaseEmbedder)
    embedder.model_name = "cl-nagoya/ruri-v3-310m"
    embedder.dimension = EMBEDDING_DIMENSION
    if raises is not None:
        embedder.embed_document = AsyncMock(side_effect=raises)
    else:
        embedder.embed_document = AsyncMock(
            return_value=vector
            or EmbeddingVector(root=tuple([0.1] * EMBEDDING_DIMENSION)),
        )
    return embedder


async def _build_article(
    db_session: AsyncSession,
    source: NewsSource,
    *,
    url: str,
    title: str = "Article",
) -> Article:
    article = Article(
        source_id=source.id,
        source_url=url,
        original_title=title,
        original_content="content body content body",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()
    return article


async def _build_extraction(
    db_session: AsyncSession,
    article: Article,
    *,
    translated_title: str = "テスト抽出タイトル",
    summary: str = "テスト抽出要約",
) -> ArticleCuration:
    extraction = ArticleCuration(
        article_id=article.id,
        translated_title=translated_title,
        summary=summary,
    )
    db_session.add(extraction)
    await db_session.flush()
    return extraction


async def _build_analysis(
    db_session: AsyncSession,
    extraction: ArticleCuration,
    category_id: int,
    *,
    translated_title: str = "分析タイトル",
    summary: str = "分析要約",
    embedding: list[float] | None = None,
) -> InScopeAssessment:
    analysis = InScopeAssessment(
        curation_id=extraction.id,
        translated_title=translated_title,
        summary=summary,
        investor_take="投資家視点",
        category_id=category_id,
        embedding=embedding,
    )
    db_session.add(analysis)
    await db_session.flush()
    return analysis


def _make_ready(
    *,
    analysis_id: int,
    article_id: int,
    text: str = "分析タイトル\n分析要約",
) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=analysis_id,
        text_for_embedding=text,
        article_id=article_id,
    )


async def _fetch_audit(db_session: AsyncSession, article_id: int) -> PipelineEvent:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, f"expected exactly 1 audit row, got {len(rows)}"
    return rows[0]


# Happy path: 永続化 + 成功 audit + None 返却


@pytest.mark.asyncio
async def test_execute_persists_embedding_on_success(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """正常系: 埋め込み生成 → 永続化 → 成功 audit 焼き付け → None 返却。"""
    article = await _build_article(
        db_session, sample_source, url="https://example.com/embed-ok"
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(db_session, extraction, sample_categories[0].id)
    await db_session.commit()
    analysis_id = analysis.id
    article_id = article.id

    embedder = _mock_embedder()
    svc = EmbeddingService(session_factory)
    ready = _make_ready(analysis_id=analysis_id, article_id=article_id)
    result = await svc.execute(ready, embedder)

    assert result is None
    embedder.embed_document.assert_called_once_with(ready)

    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    assert refetched.embedding is not None

    # 成功 audit が 1 行焼かれていること
    ev = await _fetch_audit(db_session, article_id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "embedding_completed"
    assert ev.retryability is None
    assert ev.payload["embedding_model"] == "cl-nagoya/ruri-v3-310m"
    assert ev.payload["vector_dimension"] == EMBEDDING_DIMENSION


# 並行 update で先に書かれていた: save が False → log + None で短絡


@pytest.mark.asyncio
async def test_execute_shortcircuits_when_already_persisted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """Ready 構築後に他ワーカーが先に書き込んだ場合、save は False を返し
    Service が log + None で短絡する。DB は先行値のまま上書きされない。
    audit / commit も呼ばない。
    """
    preexisting_vector = [0.4] * EMBEDDING_DIMENSION
    article = await _build_article(
        db_session, sample_source, url="https://example.com/concurrent"
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(
        db_session,
        extraction,
        sample_categories[0].id,
        embedding=preexisting_vector,
    )
    await db_session.commit()
    analysis_id = analysis.id
    article_id = article.id

    embedder = _mock_embedder(
        vector=EmbeddingVector(root=tuple([0.9] * EMBEDDING_DIMENSION))
    )
    svc = EmbeddingService(session_factory)
    # Ready 構築時には未 embedded だったが、その直後に他ワーカーが先に書き込んだ
    # 並行状況を再現するため Ready を直接構築して execute に渡す。
    ready = _make_ready(analysis_id=analysis_id, article_id=article_id)
    result = await svc.execute(ready, embedder)

    assert result is None
    # 先行する write の値のまま、後続の save で上書きされていない
    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    raw = refetched.embedding
    values = raw.to_list() if hasattr(raw, "to_list") else list(raw)
    assert values[0] == pytest.approx(0.4, abs=1e-3)

    # 敗者経路は audit を焼かない
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0


# ACL boundary: AIProviderError → Layer 1 marker に詰め替え


@pytest.mark.asyncio
async def test_execute_wraps_target_rejected_provider_error(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """``AIProviderInputRejectedError`` は Service の ACL で target-local marker に
    詰め替えられて raise される。

    Service は握らず Task 層に伝搬。DB / audit は変更されない。
    """
    article = await _build_article(
        db_session, sample_source, url="https://example.com/input-rejected"
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(db_session, extraction, sample_categories[0].id)
    await db_session.commit()
    analysis_id = analysis.id
    article_id = article.id

    original = AIProviderInputRejectedError(
        reason=GeminiContentRejectionReason.INPUT_BLOCKED
    )
    embedder = _mock_embedder(raises=original)
    svc = EmbeddingService(session_factory)
    ready = _make_ready(analysis_id=analysis_id, article_id=article_id)

    with pytest.raises(EmbeddingTerminalTargetRejectedError) as exc_info:
        await svc.execute(ready, embedder)

    # provider_error attr に元 instance が identity 付きで保持される
    assert exc_info.value.provider_error is original
    assert exc_info.value.code == AIProviderInputRejectedError.CODE
    # __cause__ に元 provider error が紐付く (audit error_chain 連鎖)
    assert exc_info.value.__cause__ is original

    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    assert refetched.embedding is None  # 永続化されていない


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_exc",
    [
        AIProviderRateLimitedError("rate limited"),
        AIProviderServiceUnavailableError("provider down"),
        AIProviderNetworkError("timeout"),
    ],
)
async def test_execute_wraps_recoverable_provider_errors(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
    provider_exc: Exception,
) -> None:
    """Rate / Service / Network は Service の ACL で
    ``EmbeddingRecoverableError`` に詰め替えられて Task 層に伝搬する。
    """
    article = await _build_article(
        db_session,
        sample_source,
        url=f"https://example.com/recoverable-{type(provider_exc).__name__}",
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(db_session, extraction, sample_categories[0].id)
    await db_session.commit()
    analysis_id = analysis.id
    article_id = article.id

    embedder = _mock_embedder(raises=provider_exc)
    svc = EmbeddingService(session_factory)
    ready = _make_ready(analysis_id=analysis_id, article_id=article_id)

    with pytest.raises(EmbeddingRecoverableError) as exc_info:
        await svc.execute(ready, embedder)

    assert exc_info.value.provider_error is provider_exc
    assert exc_info.value.__cause__ is provider_exc


# Layer 2-B: embedder 境界が raise した EmbeddingResponseInvalidError は
#            Service で握らずそのまま伝播する


@pytest.mark.asyncio
async def test_execute_propagates_response_invalid_from_embedder(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """``embedder.embed_document`` が境界内で VO 違反を詰め替えて raise した
    ``EmbeddingResponseInvalidError`` (Layer 2-B、Recoverable 継承) は Service の
    ACL 対象外なのでそのまま外に伝播し、永続化は行われない。
    """
    article = await _build_article(
        db_session, sample_source, url="https://example.com/vec-invalid"
    )
    extraction = await _build_extraction(db_session, article)
    analysis = await _build_analysis(db_session, extraction, sample_categories[0].id)
    await db_session.commit()
    analysis_id = analysis.id
    article_id = article.id

    # embedder 境界が VO 構造違反を Layer 2-B に詰め替えた状態を直接 mock
    response_invalid = EmbeddingResponseInvalidError()
    embedder = _mock_embedder(raises=response_invalid)
    svc = EmbeddingService(session_factory)
    ready = _make_ready(analysis_id=analysis_id, article_id=article_id)

    with pytest.raises(EmbeddingResponseInvalidError) as exc_info:
        await svc.execute(ready, embedder)

    assert exc_info.value is response_invalid
    assert exc_info.value.code == "embedding_response_invalid"
    assert exc_info.value.provider_error is None

    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    assert refetched.embedding is None  # 永続化されていない
