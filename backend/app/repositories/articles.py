"""記事向けの読み取り専用クエリ（一覧/詳細/類似）."""

from sqlalchemy import exists, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, defer, selectinload

from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.discovered_article import DiscoveredArticle
from app.models.topic import Topic
from app.schemas.articles import ArticleListParams, SortOrder


def article_eager_options_brief() -> list:
    """一覧用. 呼び出し側で extraction → article まで join 済みであること."""
    return [
        contains_eager(ArticleAnalysis.extraction)
        .contains_eager(ArticleExtraction.article)
        .options(
            defer(Article.original_content, raiseload=True),
            selectinload(Article.discovered_article).selectinload(
                DiscoveredArticle.news_source
            ),
        ),
        selectinload(ArticleAnalysis.topic),
    ]


def article_eager_options_detail() -> list:
    """詳細用. 呼び出し側で extraction → article まで join 済みであること."""
    return [
        contains_eager(ArticleAnalysis.extraction)
        .contains_eager(ArticleExtraction.article)
        .options(
            defer(Article.original_content, raiseload=True),
            selectinload(Article.discovered_article).selectinload(
                DiscoveredArticle.news_source
            ),
        ),
        selectinload(ArticleAnalysis.topic),
    ]


class ArticleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- public: listing ------------------------------------------------

    async def fetch_articles(
        self,
        query: ArticleListParams,
    ) -> tuple[list[ArticleAnalysis], int]:
        """ニュース閲覧用にページング済みの記事一覧を取得する."""
        stmt = (
            select(ArticleAnalysis)
            .join(ArticleAnalysis.extraction)
            .join(ArticleExtraction.article)
            .options(*article_eager_options_brief())
        )

        # フィルタ
        if query.category is not None:
            cat_id_sub = select(Category.id).where(Category.slug == query.category)
            topic_id_sub = select(Topic.id).where(Topic.category_id.in_(cat_id_sub))
            stmt = stmt.where(ArticleAnalysis.topic_id.in_(topic_id_sub))

        if query.impact_level is not None:
            stmt = stmt.where(ArticleAnalysis.impact_level == query.impact_level)

        # 総件数
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        # ソート
        order = (
            Article.published_at.desc()
            if query.sort_order == SortOrder.DESC
            else Article.published_at.asc()
        )
        stmt = stmt.order_by(order, ArticleAnalysis.id.desc())

        # ページング
        stmt = stmt.offset(query.offset).limit(query.limit)

        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all()), total

    async def fetch_one_analyzed(self, article_id: int) -> ArticleAnalysis | None:
        """分析情報を eager load した単一記事を取得する.

        見つからないか未分析の場合は None を返す.
        """
        stmt = (
            select(ArticleAnalysis)
            .join(ArticleAnalysis.extraction)
            .join(ArticleExtraction.article)
            .where(ArticleAnalysis.id == article_id)
            .options(*article_eager_options_detail())
        )
        result = await self.session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def exists_analyzed(self, article_id: int) -> bool:
        """分析済み記事が存在するかを判定する."""
        stmt = select(exists().where(ArticleAnalysis.id == article_id))
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def fetch_similar_to(
        self, article_id: int, limit: int
    ) -> list[ArticleAnalysis]:
        """指定記事に類似した記事を cosine distance 順で取得する.

        対象記事が存在しないか埋め込みを持たない場合は空リストを返す.
        """
        source_embedding = (
            select(ArticleAnalysis.embedding)
            .where(
                ArticleAnalysis.id == article_id,
                ArticleAnalysis.embedding.is_not(None),
            )
            .cte("source_embedding")
        )

        stmt = (
            select(ArticleAnalysis)
            .join(ArticleAnalysis.extraction)
            .join(ArticleExtraction.article)
            .join(source_embedding, true())
            .options(*article_eager_options_brief())
            .where(
                ArticleAnalysis.id != article_id,
                ArticleAnalysis.embedding.is_not(None),
            )
            .order_by(
                ArticleAnalysis.embedding.cosine_distance(source_embedding.c.embedding)
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all())
