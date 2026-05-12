"""EmbeddingService (app.analysis.embedding.service) の DB 統合テスト。

Stage 5 は pipeline 終端のため execute は副作用のみ (永続化) を行い ``None`` を
返す。Outcome / Entity 復元 / 読み戻しは廃止済み (2026-05-12)。

Service は ACL boundary を持ち、embedder が raise した ``AIProviderError`` を
``to_embedding_error`` で Stage 5 Layer 1 marker
(``EmbeddingRecoverableError`` / ``EmbeddingTerminalSkipError``) に詰め替えて
raise する。Layer 2-B (``EmbeddingResponseInvalidError``) は embedder 境界内で
詰め替え済 (BC 境界原則: feedback_bc_boundary_guarantees_downstream) で、
Service はそのまま伝播させる。

検証する経路:
- 正常系 (新規生成 → 永続化 + 成功 audit → None)
- 並行 update で先に書かれていた → log + None で短絡 (DB / audit 双方変化なし、
  actor SSoT — 勝者 task が audit を焼く)
- AI 層 Layer 2-A 例外 (``AIProviderInputRejectedError``) → ACL で
  ``EmbeddingTerminalSkipError`` に詰め替えられて raise (Service 内で握らない)
- AI 層 Recoverable 例外 (Rate/Service/Network) →
  ``EmbeddingRecoverableError`` に詰め替えられて raise
- embedder 境界が raise した ``EmbeddingResponseInvalidError`` (Layer 2-B) は
  Service で握らずそのまま raise されて Task 層 2 marker dispatch に流れる
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.errors import (
    EmbeddingRecoverableError,
    EmbeddingResponseInvalidError,
    EmbeddingTerminalSkipError,
)
from app.analysis.embedding.service import EmbeddingService
from app.analysis.errors.provider import (
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderServiceUnavailableError,
)
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_embedder(
    *,
    vector: EmbeddingVector | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    """固定 VO を返す (または例外を投げる) モック embedder。

    ``embed_document`` は永続化可能性を型レベルで保証する ``EmbeddingVector``
    を返す契約 (BC 境界原則)。VO 構造違反は embedder 内で
    ``EmbeddingResponseInvalidError`` に詰め替え済のため、Service テストでは
    valid VO を返す ``vector`` か、boundary が raise する例外を直接 mock する。
    """
    embedder = MagicMock(spec=BaseEmbedder)
    embedder.MODEL = "cl-nagoya/ruri-v3-310m"
    embedder.DIMENSION = EMBEDDING_DIMENSION
    embedder.model_name = "cl-nagoya/ruri-v3-310m"
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
) -> ArticleExtraction:
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title=translated_title,
        summary=summary,
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(extraction)
    await db_session.flush()
    return extraction


async def _build_analysis(
    db_session: AsyncSession,
    extraction: ArticleExtraction,
    category_id: int,
    *,
    translated_title: str = "分析タイトル",
    summary: str = "分析要約",
    embedding: list[float] | None = None,
) -> InScopeAssessment:
    analysis = InScopeAssessment(
        extraction_id=extraction.id,
        translated_title=translated_title,
        summary=summary,
        investor_take="投資家視点",
        ai_model="gemini-2.5-flash-lite",
        topic="embedding service",
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


# ---------------------------------------------------------------------------
# Happy path: 永続化 + 成功 audit + None 返却
# ---------------------------------------------------------------------------


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
    assert ev.category == "success"
    assert ev.code == "embedding_completed"
    assert ev.payload["embedding_model"] == "cl-nagoya/ruri-v3-310m"
    assert ev.payload["vector_dimension"] == EMBEDDING_DIMENSION


# ---------------------------------------------------------------------------
# 並行 update で先に書かれていた: save が False → log + None で短絡
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_shortcircuits_when_already_persisted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """Ready 構築後に他ワーカーが先に書き込んだ場合、save は False を返し
    Service が log + None で短絡する。DB は先行値のまま上書きされない。
    audit / commit も呼ばない (勝者 task の audit を二重記録しない、actor SSoT)。
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

    # 敗者経路は audit を焼かない (勝者 task が自身の audit を焼く、actor SSoT)
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


# ---------------------------------------------------------------------------
# ACL boundary: AIProviderError → Layer 1 marker に詰め替え
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_wraps_terminal_skip_provider_error(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """``AIProviderInputRejectedError`` (TerminalSkip 系) は Service の ACL で
    ``EmbeddingTerminalSkipError`` に詰め替えられて raise される。

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

    original = AIProviderInputRejectedError("input policy violation")
    embedder = _mock_embedder(raises=original)
    svc = EmbeddingService(session_factory)
    ready = _make_ready(analysis_id=analysis_id, article_id=article_id)

    with pytest.raises(EmbeddingTerminalSkipError) as exc_info:
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


# ---------------------------------------------------------------------------
# Layer 2-B: embedder 境界が raise した EmbeddingResponseInvalidError は
#            Service で握らずそのまま伝播する
# ---------------------------------------------------------------------------


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
    response_invalid = EmbeddingResponseInvalidError(
        "embedder returned vector violating EmbeddingVector invariants: NaN at [0]"
    )
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
