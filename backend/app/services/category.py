from app.repositories.category import CategoryRepository
from app.schemas.category import Category, CategoryStats, CategoryStatsList


class CategoryService:
    def __init__(self, repo: CategoryRepository) -> None:
        self.repo = repo

    async def list_category_stats(self) -> CategoryStatsList:
        """全カテゴリの集計値を返す。

        直近 24 時間に分析された記事が無いカテゴリは 0 件とする。
        """
        cat_rows = await self.repo.fetch_categories()
        count_rows = await self.repo.fetch_category_article_counts()

        article_counts_by_cat: dict[int, int] = {
            row.category_id: row.article_count for row in count_rows
        }

        return CategoryStatsList(
            items=[
                CategoryStats(
                    category=Category(slug=row.slug, name=row.name),
                    article_count_24h=article_counts_by_cat.get(row.id, 0),
                )
                for row in cat_rows
            ]
        )
