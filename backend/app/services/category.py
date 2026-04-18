from collections import defaultdict

from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryDetail, CategoryDetailList
from app.schemas.embeds import TopicStatEmbed


class CategoryService:
    def __init__(self, repo: CategoryRepository) -> None:
        self.repo = repo

    async def list_categories(self) -> CategoryDetailList:
        """リポジトリから取得したデータで CategoryDetailList を構築する。"""
        cat_rows = await self.repo.fetch_categories()
        topic_rows = await self.repo.fetch_topic_stats()
        count_rows = await self.repo.fetch_category_article_counts()

        # category_id -> article_count のマッピング
        article_counts_by_cat: dict[int, int] = {
            row.category_id: row.article_count for row in count_rows
        }

        # トピックを category_id でグルーピングする
        topic_stats_by_cat: dict[int, list[TopicStatEmbed]] = defaultdict(list)
        for row in topic_rows:
            topic_stats_by_cat[row.category_id].append(
                TopicStatEmbed(
                    name=row.name,
                    article_count=row.article_count,
                )
            )

        return CategoryDetailList(
            items=[
                CategoryDetail(
                    slug=row.slug,
                    name=row.name,
                    article_count=article_counts_by_cat.get(row.id, 0),
                    topics=topic_stats_by_cat.get(row.id, []),
                )
                for row in cat_rows
            ]
        )
