"""Embedding tasks — vector embedding generation."""

from __future__ import annotations

import structlog
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import Context, TaskiqDepends

from app.ai.embedding import _build_embed_text, get_embedder
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle
from app.tasks.brokers import broker_embedding

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@broker_embedding.task(
    task_name="generate_embedding",
    timeout=60,
    max_retries=2,
    retry_on_error=True,
)
async def generate_embedding(
    article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """Generate vector embedding for a single article's analysis."""
    engine = ctx.state.engine

    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        analysis = (
            await session.execute(
                select(ArticleAnalysis).where(
                    ArticleAnalysis.news_article_id == article_id
                )
            )
        ).scalar_one_or_none()

        if analysis is None:
            logger.warning("generate_embedding_no_analysis", article_id=article_id)
            return

        # Idempotency guard
        if analysis.embedding is not None:
            return

        article = await session.get(NewsArticle, article_id)
        if article is None:
            return

        embedder = get_embedder()
        text = _build_embed_text(article)
        vector = await embedder.embed_document(text)

        analysis.embedding = vector
        analysis.embedding_model = embedder.MODEL
        session.add(analysis)
        await session.commit()

    logger.info("generate_embedding_completed", article_id=article_id)
