"""記事向けの読み取り専用クエリ（一覧/詳細/類似）。"""

from sqlalchemy import exists, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, defer, load_only, selectinload

from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource
from app.schemas.articles import ArticleListParams


def article_eager_options_brief() -> list:
    """一覧用. 呼び出し側で curation → article まで join 済みであること."""
    return [
        load_only(
            AnalyzedArticleRecord.id,
            AnalyzedArticleRecord.curation_id,
            AnalyzedArticleRecord.category_id,
            AnalyzedArticleRecord.translated_title,
            AnalyzedArticleRecord.summary,
            AnalyzedArticleRecord.key_points,
            raiseload=True,
        ),
        contains_eager(AnalyzedArticleRecord.curation)
        .load_only(
            ArticleCuration.id,
            ArticleCuration.analyzable_article_id,
            raiseload=True,
        )
        .contains_eager(ArticleCuration.analyzable_article)
        .options(
            load_only(
                AnalyzableArticleRecord.id,
                AnalyzableArticleRecord.source_id,
                AnalyzableArticleRecord.published_at,
                raiseload=True,
            ),
            selectinload(AnalyzableArticleRecord.news_source).load_only(
                NewsSource.name,
                NewsSource.attribution_label,
                raiseload=True,
            ),
        ),
        # category は AnalyzedArticleRecord ルート相対なので上の chain には入れない.
        selectinload(AnalyzedArticleRecord.category),
    ]


def article_eager_options_detail() -> list:
    """詳細用. 呼び出し側で curation → article まで join 済みであること."""
    return [
        contains_eager(AnalyzedArticleRecord.curation)
        .contains_eager(ArticleCuration.analyzable_article)
        .options(
            defer(AnalyzableArticleRecord.original_content, raiseload=True),
            selectinload(AnalyzableArticleRecord.news_source),
        ),
        # detail も category を返す. async では未 load の relationship 参照が
        # lazy load で MissingGreenlet を投げるため eager load 必須.
        selectinload(AnalyzedArticleRecord.category),
    ]


class ArticleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- public: listing ------------------------------------------------

    async def fetch_articles(
        self,
        query: ArticleListParams,
        limit: int,
    ) -> list[AnalyzedArticleRecord]:
        """ニュース閲覧用に、カーソルの位置より後の記事を新しい順に取得する."""
        stmt = (
            select(AnalyzedArticleRecord)
            .join(AnalyzedArticleRecord.curation)
            .join(ArticleCuration.analyzable_article)
            .options(*article_eager_options_brief())
        )

        if query.category is not None:
            cat_id_sub = select(Category.id).where(Category.slug == query.category)
            stmt = stmt.where(AnalyzedArticleRecord.category_id.in_(cat_id_sub))

        if query.cursor is not None:
            # 行比較は2表にまたがり索引の開始位置にならないため、published_at の範囲を
            # 併記して idx_analyzable_articles_published をカーソルの位置から読ませる。
            stmt = stmt.where(
                AnalyzableArticleRecord.published_at <= query.cursor.published_at,
                tuple_(AnalyzableArticleRecord.published_at, AnalyzedArticleRecord.id)
                < tuple_(query.cursor.published_at, query.cursor.id),
            )

        # published_at は NOT NULL (ドメイン不変条件 + DB 制約)。
        stmt = stmt.order_by(
            AnalyzableArticleRecord.published_at.desc(),
            AnalyzedArticleRecord.id.desc(),
        ).limit(limit)

        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all())

    async def fetch_one_analyzed(self, article_id: int) -> AnalyzedArticleRecord | None:
        """分析情報を eager load した単一記事を取得する.

        見つからないか未分析の場合は None を返す.
        """
        stmt = (
            select(AnalyzedArticleRecord)
            .join(AnalyzedArticleRecord.curation)
            .join(ArticleCuration.analyzable_article)
            .where(AnalyzedArticleRecord.id == article_id)
            .options(*article_eager_options_detail())
        )
        result = await self.session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def exists_analyzed(self, article_id: int) -> bool:
        """分析済み記事が存在するかを判定する."""
        stmt = select(exists().where(AnalyzedArticleRecord.id == article_id))
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def fetch_similar_to(
        self, article_id: int, limit: int
    ) -> list[AnalyzedArticleRecord]:
        """指定記事に類似した記事を cosine distance 順で取得する.

        対象記事が存在しないか埋め込みを持たない場合は空リストを返す.
        """
        source_embedding = (
            select(AnalyzedArticleRecord.embedding)
            .where(
                AnalyzedArticleRecord.id == article_id,
                AnalyzedArticleRecord.embedding.is_not(None),
            )
            .cte("source_embedding")
        )

        stmt = (
            select(AnalyzedArticleRecord)
            .join(AnalyzedArticleRecord.curation)
            .join(ArticleCuration.analyzable_article)
            .join(source_embedding, true())
            .options(*article_eager_options_brief())
            .where(
                AnalyzedArticleRecord.id != article_id,
                AnalyzedArticleRecord.embedding.is_not(None),
            )
            .order_by(
                AnalyzedArticleRecord.embedding.cosine_distance(
                    source_embedding.c.embedding
                )
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all())
