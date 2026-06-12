"""記事向けの読み取り専用クエリ（一覧/詳細/類似）+ DELETE."""

from sqlalchemy import delete, exists, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, defer, selectinload

from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.schemas.articles import ArticleListParams, SortOrder


def article_eager_options_brief() -> list:
    """一覧用. 呼び出し側で curation → article まで join 済みであること."""
    return [
        contains_eager(InScopeAssessment.curation)
        .contains_eager(ArticleCuration.article)
        .options(
            defer(Article.original_content, raiseload=True),
            selectinload(Article.news_source),
        ),
        # category は InScopeAssessment ルート相対なので上の chain には入れない.
        selectinload(InScopeAssessment.category),
    ]


def article_eager_options_detail() -> list:
    """詳細用. 呼び出し側で curation → article まで join 済みであること."""
    return [
        contains_eager(InScopeAssessment.curation)
        .contains_eager(ArticleCuration.article)
        .options(
            defer(Article.original_content, raiseload=True),
            selectinload(Article.news_source),
        ),
        # detail も category を返す. async では未 load の relationship 参照が
        # lazy load で MissingGreenlet を投げるため eager load 必須.
        selectinload(InScopeAssessment.category),
    ]


class ArticleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- public: listing ------------------------------------------------

    async def fetch_articles(
        self,
        query: ArticleListParams,
    ) -> tuple[list[InScopeAssessment], int]:
        """ニュース閲覧用にページング済みの記事一覧を取得する."""
        stmt = (
            select(InScopeAssessment)
            .join(InScopeAssessment.curation)
            .join(ArticleCuration.article)
            .options(*article_eager_options_brief())
        )

        # フィルタ
        if query.category is not None:
            cat_id_sub = select(Category.id).where(Category.slug == query.category)
            stmt = stmt.where(InScopeAssessment.category_id.in_(cat_id_sub))

        # 総件数
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        # ソート。日付不明 (published_at null) は方向に依らず末尾へ
        # (PostgreSQL の DESC 既定は NULLS FIRST で新着の先頭を占有するため)。
        order = (
            Article.published_at.desc().nulls_last()
            if query.sort_order == SortOrder.DESC
            else Article.published_at.asc().nulls_last()
        )
        stmt = stmt.order_by(order, InScopeAssessment.id.desc())

        # ページング
        stmt = stmt.offset(query.offset).limit(query.limit)

        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all()), total

    async def fetch_one_analyzed(self, article_id: int) -> InScopeAssessment | None:
        """分析情報を eager load した単一記事を取得する.

        見つからないか未分析の場合は None を返す.
        """
        stmt = (
            select(InScopeAssessment)
            .join(InScopeAssessment.curation)
            .join(ArticleCuration.article)
            .where(InScopeAssessment.id == article_id)
            .options(*article_eager_options_detail())
        )
        result = await self.session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def exists_analyzed(self, article_id: int) -> bool:
        """分析済み記事が存在するかを判定する."""
        stmt = select(exists().where(InScopeAssessment.id == article_id))
        result = await self.session.execute(stmt)
        return result.scalar_one()

    # -- public: DELETE -------------------------------------------------

    async def delete_by_id(self, article_id: int) -> int:
        """指定 ID の記事を物理削除する。

        ``ondelete=CASCADE`` により ``article_curations`` /
        ``curation_noises`` を経由して ``in_scope_assessments`` /
        ``out_of_scope_assessments`` まで削除される。
        ``pipeline_events.article_id`` は ``ondelete=SET NULL``
        のため監査行は残り、``source_id`` で起点ソースを追跡可能。

        commit は呼出側責務 (audit INSERT と同 tx でまとめる用途を想定)。

        Returns:
            削除された行数 (0 または 1)。
        """
        result = await self.session.execute(
            delete(Article).where(Article.id == article_id)
        )
        return result.rowcount or 0

    async def fetch_similar_to(
        self, article_id: int, limit: int
    ) -> list[InScopeAssessment]:
        """指定記事に類似した記事を cosine distance 順で取得する.

        対象記事が存在しないか埋め込みを持たない場合は空リストを返す.
        """
        source_embedding = (
            select(InScopeAssessment.embedding)
            .where(
                InScopeAssessment.id == article_id,
                InScopeAssessment.embedding.is_not(None),
            )
            .cte("source_embedding")
        )

        stmt = (
            select(InScopeAssessment)
            .join(InScopeAssessment.curation)
            .join(ArticleCuration.article)
            .join(source_embedding, true())
            .options(*article_eager_options_brief())
            .where(
                InScopeAssessment.id != article_id,
                InScopeAssessment.embedding.is_not(None),
            )
            .order_by(
                InScopeAssessment.embedding.cosine_distance(
                    source_embedding.c.embedding
                )
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all())
