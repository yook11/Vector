from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryDetail, CategoryDetailList


class CategoryService:
    def __init__(self, repo: CategoryRepository) -> None:
        self.repo = repo

    async def list_categories(self) -> CategoryDetailList:
        """リポジトリから取得したデータで CategoryDetailList を構築する。"""
        cat_rows = await self.repo.fetch_categories()
        count_rows = await self.repo.fetch_category_article_counts()

        # category_id -> recent_count のマッピング
        recent_counts_by_cat: dict[int, int] = {
            row.category_id: row.recent_count for row in count_rows
        }

        return CategoryDetailList(
            items=[
                CategoryDetail(
                    slug=row.slug,
                    name=row.name,
                    recent_count=recent_counts_by_cat.get(row.id, 0),
                )
                for row in cat_rows
            ]
        )
