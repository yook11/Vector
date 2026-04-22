"""記事閲覧サービス — 一覧/詳細/類似記事。"""

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
from app.schemas.embeds import NewsSourceEmbed, OriginalArticleEmbed, TopicEmbed


def build_topic_embed(analysis: ArticleAnalysis) -> TopicEmbed:
    return TopicEmbed(name=analysis.topic.name)


def build_brief(
    analysis: ArticleAnalysis,
    watched_ids: set[int] | None = None,
) -> ArticleBrief:
    a = analysis.extraction.article
    return ArticleBrief(
        id=analysis.id,
        translated_title=analysis.translated_title,
        summary=analysis.summary,
        impact_level=analysis.impact_level,
        source=NewsSourceEmbed(name=a.news_source.name),
        published_at=a.published_at,
        topic=build_topic_embed(analysis),
        is_watched=analysis.id in watched_ids if watched_ids else False,
    )


def build_detail(
    analysis: ArticleAnalysis,
    watched_ids: set[int] | None = None,
) -> ArticleDetail:
    a = analysis.extraction.article
    return ArticleDetail(
        id=analysis.id,
        translated_title=analysis.translated_title,
        summary=analysis.summary,
        impact_level=analysis.impact_level,
        reasoning=analysis.reasoning,
        analyzed_at=analysis.analyzed_at,
        source=NewsSourceEmbed(name=a.news_source.name),
        published_at=a.published_at,
        topic=build_topic_embed(analysis),
        is_watched=analysis.id in watched_ids if watched_ids else False,
        original=OriginalArticleEmbed(
            title=a.original_title,
            url=a.original_url,
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
        """ニュース閲覧用に分析済み記事を一覧取得する。"""
        analyses, total = await self.repo.fetch_articles(query)

        watched_ids: set[int] = set()
        if user_id and analyses:
            article_ids = {a.id for a in analyses}
            watched_ids = await self.watchlist_repo.watched_among(user_id, article_ids)

        return PaginatedArticleResponse.create(
            items=[build_brief(a, watched_ids) for a in analyses],
            total=total,
            pagination=query,
        )

    async def get_article(self, article_id: int, user_id: int | None) -> ArticleDetail:
        analysis = await self.repo.fetch_one_analyzed(article_id)
        if analysis is None:
            raise NotFoundError("News article not found")

        watched_ids: set[int] = set()
        if user_id:
            watched_ids = await self.watchlist_repo.watched_among(
                user_id, {analysis.id}
            )
        return build_detail(analysis, watched_ids)

    async def get_similar(self, article_id: int, limit: int) -> list[ArticleBrief]:
        """意味的に類似する記事を検索する。"""
        analyses = await self.repo.fetch_similar_to(article_id, limit)
        return [build_brief(a) for a in analyses]
