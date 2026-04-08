"""Article reading service — list, detail, similar."""

from app.exceptions import NotFoundError
from app.models.news_article import NewsArticle
from app.repositories.articles import ArticleRepository
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import (
    ArticleBrief,
    ArticleDetail,
    ArticleListParams,
    PaginatedArticleResponse,
)
from app.schemas.embeds import KeywordEmbed, NewsSourceEmbed, OriginalArticleEmbed


def build_keyword_embeds(article: NewsArticle) -> list[KeywordEmbed]:
    return [
        KeywordEmbed(name=link.keyword.name)
        for link in article.article_keywords
        if link.keyword
    ]


def build_brief(
    article: NewsArticle,
    watched_ids: set[int] | None = None,
) -> ArticleBrief:
    a = article.article_analysis
    return ArticleBrief(
        id=a.id,
        translated_title=a.translated_title,
        summary=a.summary,
        impact_level=a.impact_level,
        source=NewsSourceEmbed(name=article.news_source.name),
        published_at=article.published_at,
        keywords=build_keyword_embeds(article),
        is_watched=a.id in watched_ids if watched_ids else False,
    )


def build_detail(
    article: NewsArticle,
    watched_ids: set[int] | None = None,
) -> ArticleDetail:
    a = article.article_analysis
    return ArticleDetail(
        id=a.id,
        translated_title=a.translated_title,
        summary=a.summary,
        impact_level=a.impact_level,
        reasoning=a.reasoning,
        analyzed_at=a.analyzed_at,
        source=NewsSourceEmbed(name=article.news_source.name),
        published_at=article.published_at,
        keywords=build_keyword_embeds(article),
        is_watched=a.id in watched_ids if watched_ids else False,
        original=OriginalArticleEmbed(
            title=article.original_title,
            url=article.original_url,
            content=article.original_content,
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
        articles, total = await self.repo.fetch_articles(query)

        watched_ids = (
            await self.watchlist_repo.get_watched_ids(user_id) if user_id else set()
        )

        return PaginatedArticleResponse.create(
            items=[build_brief(a, watched_ids) for a in articles],
            total=total,
            pagination=query,
        )

    async def get_article(self, article_id: int, user_id: int | None) -> ArticleDetail:
        article = await self.repo.fetch_one_analyzed(article_id)
        if article is None:
            raise NotFoundError("News article not found")

        watched_ids = (
            await self.watchlist_repo.get_watched_ids(user_id) if user_id else set()
        )
        return build_detail(article, watched_ids)

    async def get_similar(self, article_id: int, limit: int) -> list[ArticleBrief]:
        """Find semantically similar articles."""
        articles = await self.repo.fetch_similar_to(article_id, limit)
        return [build_brief(a) for a in articles]
