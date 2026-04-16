"""Embedding service — vector generation and DB persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.repository import AnalysisRepository
from app.models.news_article import NewsArticle

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    """Result of embedding generation use case."""

    status: Literal["created", "already_exists"]


def build_embed_text(article: NewsArticle) -> str:
    """Build the canonical text to embed for a news article."""
    body = article.original_content or article.original_description or ""
    return f"{article.original_title}\n{body}"


class EmbeddingService:
    """Atomic use case: generate embedding for a single article and persist.

    Session management is internal — callers provide only a session factory.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, article_id: int, embedder: BaseEmbedder) -> EmbeddingResult:
        """Generate embedding for a single article's analysis.

        Returns:
            EmbeddingResult with status.

        Raises:
            AnalysisDomainError subclasses — caller must handle retry decisions.
        """
        async with self._session_factory() as session:
            repo = AnalysisRepository(session)

            # Analysis must exist (chained from analyze_article)
            analysis = await repo.find_by_article_id(article_id)
            if analysis is None:
                msg = f"No analysis found for article {article_id}"
                raise ValueError(msg)

            # Idempotency check
            if analysis.embedding is not None:
                return EmbeddingResult("already_exists")

            # Fetch article for text
            article = await repo.get_article(article_id)
            if article is None:
                msg = f"Article {article_id} not found"
                raise ValueError(msg)

            # Generate embedding (all errors propagate to Task)
            text = build_embed_text(article)
            vector = await embedder.embed_document(text)

            # Persist
            await repo.save_embedding(analysis, vector, embedder.MODEL)
            await session.commit()

            logger.info(
                "embedding_completed",
                article_id=article_id,
                model=embedder.MODEL,
            )
            return EmbeddingResult("created")
