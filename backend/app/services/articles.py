"""記事閲覧サービス — 一覧/詳細/類似記事。"""

from app.exceptions import NotFoundError
from app.models.article_analysis import ArticleAnalysis
from app.repositories.articles import ArticleRepository
from app.schemas.articles import (
    ArticleBrief,
    ArticleDetail,
    ArticleListParams,
    PaginatedArticleResponse,
)
from app.schemas.embeds import NewsSourceEmbed, OriginalArticleEmbed


def build_brief(analysis: ArticleAnalysis) -> ArticleBrief:
    a = analysis.extraction.article
    return ArticleBrief(
        id=analysis.id,
        translated_title=analysis.translated_title,
        summary=analysis.summary,
        source=NewsSourceEmbed(name=a.news_source.name),
        published_at=a.published_at,
        topic=str(analysis.topic),
    )


def build_detail(analysis: ArticleAnalysis) -> ArticleDetail:
    a = analysis.extraction.article
    return ArticleDetail(
        id=analysis.id,
        translated_title=analysis.translated_title,
        summary=analysis.summary,
        investor_take=analysis.investor_take,
        analyzed_at=analysis.analyzed_at,
        source=NewsSourceEmbed(name=a.news_source.name),
        published_at=a.published_at,
        topic=str(analysis.topic),
        original=OriginalArticleEmbed(
            title=a.original_title,
            url=a.original_url,
        ),
    )


class ArticleService:
    def __init__(self, repo: ArticleRepository) -> None:
        self.repo = repo

    async def list_articles(
        self,
        query: ArticleListParams,
    ) -> PaginatedArticleResponse:
        """ニュース閲覧用に分析済み記事を一覧取得する。"""
        analyses, total = await self.repo.fetch_articles(query)
        return PaginatedArticleResponse.create(
            items=[build_brief(a) for a in analyses],
            total=total,
            pagination=query,
        )

    async def get_article(self, article_id: int) -> ArticleDetail:
        analysis = await self.repo.fetch_one_analyzed(article_id)
        if analysis is None:
            raise NotFoundError("News article not found")
        return build_detail(analysis)

    async def get_similar(self, article_id: int, limit: int) -> list[ArticleBrief]:
        """意味的に類似する記事を検索する。"""
        analyses = await self.repo.fetch_similar_to(article_id, limit)
        return [build_brief(a) for a in analyses]
