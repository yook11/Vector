"""Article reading service — list, detail, similar."""

import math

from app.exceptions import NotFoundError
from app.models.news_article import NewsArticle
from app.repositories.articles import ArticleRepository
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
        id=article.id,
        translated_title=a.translated_title,
        summary=a.summary,
        impact_level=a.impact_level,
        source=NewsSourceEmbed(name=article.news_source.name),
        published_at=article.published_at,
        keywords=build_keyword_embeds(article),
        is_watched=article.id in watched_ids if watched_ids else False,
    )


def build_detail(
    article: NewsArticle,
    watched_ids: set[int] | None = None,
) -> ArticleDetail:
    a = article.article_analysis
    return ArticleDetail(
        id=article.id,
        translated_title=a.translated_title,
        summary=a.summary,
        impact_level=a.impact_level,
        reasoning=a.reasoning,
        analyzed_at=a.analyzed_at,
        source=NewsSourceEmbed(name=article.news_source.name),
        published_at=article.published_at,
        keywords=build_keyword_embeds(article),
        is_watched=article.id in watched_ids if watched_ids else False,
        original=OriginalArticleEmbed(
            title=article.original_title,
            url=article.original_url,
            content=article.original_content,
        ),
    )


class ArticleService:
    def __init__(self, repo: ArticleRepository) -> None:
        self.repo = repo

    async def list_articles(
        self,
        query: ArticleListParams,
        user_id: int | None,
    ) -> PaginatedArticleResponse:
        """List analyzed articles for news browsing."""
        articles, total = await self.repo.fetch_articles(query)

        watched_ids = await self.repo.get_watched_ids(user_id) if user_id else set()

        return PaginatedArticleResponse(
            items=[build_brief(a, watched_ids) for a in articles],
            total=total,
            page=query.page,
            per_page=query.per_page,
            total_pages=math.ceil(total / query.per_page) if total > 0 else 0,
        )

    async def get_article(self, news_id: int, user_id: int | None) -> ArticleDetail:
        article = await self.repo.fetch_one_analyzed(news_id)
        if article is None:
            raise NotFoundError("News article not found")

        watched_ids = await self.repo.get_watched_ids(user_id) if user_id else set()
        return build_detail(article, watched_ids)

    async def get_similar(self, news_id: int, limit: int) -> list[ArticleBrief]:
        """Find semantically similar articles."""
        articles = await self.repo.fetch_similar_to(news_id, limit)
        return [build_brief(a) for a in articles]
