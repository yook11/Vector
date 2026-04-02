import math

from app.exceptions import NotFoundError
from app.models.news_article import NewsArticle
from app.repositories.news import NewsListParams, NewsRepository
from app.schemas.embeds import KeywordEmbed, NewsSourceEmbed, OriginalArticleEmbed
from app.schemas.news import (
    EmbedResponse,
    NewsBrief,
    NewsDetail,
    NewsFetchResponse,
    PaginatedNewsResponse,
)
from app.services.embedding import embed_articles, embed_search_query
from app.tasks.pipeline_tasks import fetch_metadata


class NewsService:
    def __init__(self, repo: NewsRepository) -> None:
        self.repo = repo

    @staticmethod
    def _build_keyword_embeds(article: NewsArticle) -> list[KeywordEmbed]:
        return [
            KeywordEmbed(id=link.keyword.id, name=link.keyword.name)
            for link in article.article_keywords
            if link.keyword
        ]

    @staticmethod
    def _build_brief(
        article: NewsArticle,
        watched_ids: set[int] | None = None,
    ) -> NewsBrief:
        a = article.article_analysis
        return NewsBrief(
            id=article.id,
            translated_title=a.translated_title,
            summary=a.summary,
            impact_level=a.impact_level,
            source=NewsSourceEmbed(
                id=article.news_source.id,
                name=article.news_source.name,
            ),
            published_at=article.published_at,
            keywords=NewsService._build_keyword_embeds(article),
            is_watched=article.id in watched_ids if watched_ids else False,
        )

    @staticmethod
    def _build_detail(
        article: NewsArticle,
        watched_ids: set[int] | None = None,
    ) -> NewsDetail:
        a = article.article_analysis
        return NewsDetail(
            id=article.id,
            translated_title=a.translated_title,
            summary=a.summary,
            impact_level=a.impact_level,
            reasoning=a.reasoning,
            analyzed_at=a.analyzed_at,
            source=NewsSourceEmbed(
                id=article.news_source.id,
                name=article.news_source.name,
            ),
            published_at=article.published_at,
            keywords=NewsService._build_keyword_embeds(article),
            is_watched=article.id in watched_ids if watched_ids else False,
            original=OriginalArticleEmbed(
                title=article.original_title,
                url=article.original_url,
                content=article.original_content,
            ),
        )

    async def list_news(
        self,
        params: NewsListParams,
        q: str | None,
        user_id: int | None,
    ) -> PaginatedNewsResponse:
        """List analyzed news with optional semantic search."""
        query_embedding: list[float] | None = None
        if q is not None:
            query_embedding = await embed_search_query(q)

        articles, total = await self.repo.fetch_analyzed_list(params, query_embedding)

        watched_ids = await self.repo.get_watched_ids(user_id) if user_id else set()

        return PaginatedNewsResponse(
            items=[self._build_brief(a, watched_ids) for a in articles],
            total=total,
            page=params.page,
            per_page=params.per_page,
            total_pages=math.ceil(total / params.per_page) if total > 0 else 0,
        )

    async def get_news(self, news_id: int, user_id: int | None) -> NewsDetail:
        article = await self.repo.fetch_one_analyzed(news_id)
        if article is None:
            raise NotFoundError("News article not found")

        watched_ids = await self.repo.get_watched_ids(user_id) if user_id else set()
        return self._build_detail(article, watched_ids)

    async def get_similar(self, news_id: int, limit: int) -> list[NewsBrief]:
        """Find semantically similar articles.

        Returns empty list if article has no embedding.
        Raises NotFoundError if article does not exist.
        """
        analysis = await self.repo.get_analysis(news_id)

        if analysis is None or analysis.embedding is None:
            if not await self.repo.article_exists(news_id):
                raise NotFoundError("News article not found")
            return []

        articles = await self.repo.fetch_similar(analysis.embedding, news_id, limit)
        return [self._build_brief(a) for a in articles]

    async def embed_news(self) -> EmbedResponse:
        analyses = await self.repo.get_analyses_without_embedding()

        if not analyses:
            return EmbedResponse(
                message="No analyses need embedding",
                embedded_count=0,
                skipped_count=0,
                error_count=0,
            )

        er = await embed_articles(self.repo.session, analyses)

        return EmbedResponse(
            message=f"Embedding completed: {er.embedded_count} embedded, "
            f"{er.error_count} errors",
            embedded_count=er.embedded_count,
            skipped_count=er.skipped_count,
            error_count=er.error_count,
        )

    @staticmethod
    async def fetch_news(source_ids: list[int] | None) -> NewsFetchResponse:
        task = await fetch_metadata.kiq(source_ids=source_ids)
        return NewsFetchResponse(
            message="Fetch task submitted",
            sources_count=len(source_ids) if source_ids else None,
            job_id=task.task_id,
        )
