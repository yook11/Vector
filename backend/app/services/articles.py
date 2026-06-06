"""記事閲覧サービス — 一覧/詳細/類似記事。"""

from typing import Any

from app.exceptions import NotFoundError
from app.models.in_scope_assessment import InScopeAssessment
from app.repositories.articles import ArticleRepository
from app.schemas.articles import (
    ArticleBrief,
    ArticleDetail,
    ArticleListParams,
    PaginatedArticleResponse,
)
from app.schemas.embeds import CategoryEmbed, NewsSourceEmbed, OriginalArticleEmbed


def build_brief(analysis: InScopeAssessment) -> ArticleBrief:
    a = analysis.curation.article
    return ArticleBrief(
        id=analysis.id,
        translated_title=analysis.translated_title,
        summary=analysis.summary,
        category=CategoryEmbed(
            slug=analysis.category.slug,
            name=analysis.category.name,
        ),
        source=NewsSourceEmbed(
            name=a.news_source.name,
            attribution_label=a.news_source.attribution_label,
        ),
        published_at=a.published_at,
    )


def _extract_key_point_contents(key_points: list[dict[str, Any]] | None) -> list[str]:
    """JSONB key_points から表示用の content 文字列だけを取り出す。

    mentions は API 非公開 (trends 内部利用) のため落とす。NULL/空、content 欠落・
    非 str・空文字の要素は除外して常に ``list[str]`` を返す。
    """
    if not key_points:
        return []
    return [
        kp["content"]
        for kp in key_points
        if isinstance(kp, dict) and isinstance(kp.get("content"), str) and kp["content"]
    ]


def build_detail(analysis: InScopeAssessment) -> ArticleDetail:
    a = analysis.curation.article
    return ArticleDetail(
        id=analysis.id,
        translated_title=analysis.translated_title,
        summary=analysis.summary,
        investor_take=analysis.investor_take,
        key_points=_extract_key_point_contents(analysis.key_points),
        analyzed_at=analysis.analyzed_at,
        category=CategoryEmbed(
            slug=analysis.category.slug,
            name=analysis.category.name,
        ),
        source=NewsSourceEmbed(
            name=a.news_source.name,
            attribution_label=a.news_source.attribution_label,
        ),
        published_at=a.published_at,
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
        if not await self.repo.exists_analyzed(article_id):
            raise NotFoundError("News article not found")
        analyses = await self.repo.fetch_similar_to(article_id, limit)
        return [build_brief(a) for a in analyses]
