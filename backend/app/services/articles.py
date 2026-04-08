"""Article reading service — list, detail, similar."""

from app.exceptions import NotFoundError
from app.models.article_analysis import ArticleAnalysis
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import (
    ArticleBrief,
    ArticleDetail,
    ArticleListParams,
    PaginatedArticleResponse,
)
from app.schemas.embeds import KeywordEmbed, NewsSourceEmbed, OriginalArticleEmbed


def build_keyword_embeds(analysis: ArticleAnalysis) -> list[KeywordEmbed]:
    return [
        KeywordEmbed(name=link.keyword.name)
        for link in analysis.article_keywords
        if link.keyword
    ]


def build_brief(
    analysis: ArticleAnalysis,
    watched_ids: set[int] | None = None,
) -> ArticleBrief:
    na = analysis.news_article
    return ArticleBrief(
        id=analysis.id,
        translated_title=analysis.translated_title,
        summary=analysis.summary,
        impact_level=analysis.impact_level,
        source=NewsSourceEmbed(name=na.news_source.name),
        published_at=na.published_at,
        keywords=build_keyword_embeds(analysis),
        is_watched=analysis.id in watched_ids if watched_ids else False,
    )


def build_detail(
    analysis: ArticleAnalysis,
    watched_ids: set[int] | None = None,
) -> ArticleDetail:
    na = analysis.news_article
    return ArticleDetail(
        id=analysis.id,
        translated_title=analysis.translated_title,
        summary=analysis.summary,
        impact_level=analysis.impact_level,
        reasoning=analysis.reasoning,
        analyzed_at=analysis.analyzed_at,
        source=NewsSourceEmbed(name=na.news_source.name),
        published_at=na.published_at,
        keywords=build_keyword_embeds(analysis),
        is_watched=analysis.id in watched_ids if watched_ids else False,
        original=OriginalArticleEmbed(
            title=na.original_title,
            url=na.original_url,
            content=na.original_content,
        ),
    )


class ArticleService:
    def __init__(
        self,
        repo: ArticleRepository,
        watchlist_repo: WatchlistRepository,
    ) -> None:
        self.repo = repo
        self.watchlist_repo = watchlist_repo

    async def list_articles(
        self,
        query: ArticleListParams,
        user_id: int | None,
    ) -> PaginatedArticleResponse:
        """List analyzed articles for news browsing."""
        analyses, total = await self.repo.fetch_articles(query)

        watched_ids = (
            await self.watchlist_repo.get_watched_ids(user_id) if user_id else set()
        )

        return PaginatedArticleResponse.create(
            items=[build_brief(a, watched_ids) for a in analyses],
            total=total,
            pagination=query,
        )

    async def get_article(self, article_id: int, user_id: int | None) -> ArticleDetail:
        analysis = await self.repo.fetch_one_analyzed(article_id)
        if analysis is None:
            raise NotFoundError("News article not found")

        watched_ids = (
            await self.watchlist_repo.get_watched_ids(user_id) if user_id else set()
        )
        return build_detail(analysis, watched_ids)

    async def get_similar(self, article_id: int, limit: int) -> list[ArticleBrief]:
        """Find semantically similar articles."""
        analyses = await self.repo.fetch_similar_to(article_id, limit)
        return [build_brief(a) for a in analyses]
