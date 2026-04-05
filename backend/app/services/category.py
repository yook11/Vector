from collections import defaultdict

from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryDetail, CategoryDetailList
from app.schemas.embeds import KeywordStatEmbed


class CategoryService:
    def __init__(self, repo: CategoryRepository) -> None:
        self.repo = repo

    async def list_categories(self) -> CategoryDetailList:
        """Build CategoryDetailList from repository data."""
        cat_rows = await self.repo.fetch_categories()
        kw_rows = await self.repo.fetch_keyword_stats()
        count_rows = await self.repo.fetch_category_article_counts()

        # category_id -> article_count
        article_counts_by_cat: dict[int, int] = {
            row.category_id: row.article_count for row in count_rows
        }

        # Group keywords by category_id
        keyword_stats_by_cat: dict[int, list[KeywordStatEmbed]] = defaultdict(list)
        for row in kw_rows:
            keyword_stats_by_cat[row.category_id].append(
                KeywordStatEmbed(
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
                    keywords=keyword_stats_by_cat.get(row.id, []),
                )
                for row in cat_rows
            ]
        )
