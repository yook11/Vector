from app.exceptions import DuplicateError, NotFoundError, ReferenceNotFoundError
from app.models.keyword import Keyword
from app.repositories.keyword import KeywordRepository
from app.schemas.embeds import CategoryEmbed
from app.schemas.keyword import (
    KeywordCreate,
    KeywordDetail,
    KeywordDetailList,
    KeywordUpdate,
)


class KeywordService:
    def __init__(self, repo: KeywordRepository) -> None:
        self.repo = repo

    @staticmethod
    def _build_detail(keyword: Keyword, article_count: int) -> KeywordDetail:
        """Build KeywordDetail from ORM object and computed article count."""
        return KeywordDetail(
            id=keyword.id,
            name=keyword.name,
            category=CategoryEmbed.model_validate(keyword.category),
            status=keyword.status,
            article_count=article_count,
            created_at=keyword.created_at,
        )

    async def list_keywords(self) -> KeywordDetailList:
        rows = await self.repo.fetch_all_with_stats()
        return KeywordDetailList(
            items=[self._build_detail(kw, count) for kw, count in rows]
        )

    async def create_keyword(self, body: KeywordCreate) -> KeywordDetail:
        if await self.repo.get_by_name(body.name):
            raise DuplicateError("Keyword already exists")
        if not await self.repo.category_exists(body.category_id):
            raise ReferenceNotFoundError(f"Category ID {body.category_id} not found")

        keyword = Keyword(name=body.name, category_id=body.category_id)
        keyword = await self.repo.create(keyword)
        kw, count = await self.repo.fetch_one_with_stats(keyword.id)
        return self._build_detail(kw, count)

    async def update_keyword(
        self, keyword_id: int, body: KeywordUpdate
    ) -> KeywordDetail:
        keyword = await self.repo.get_by_id(keyword_id)
        if not keyword:
            raise NotFoundError("Keyword not found")
        if body.category_id is not None:
            if not await self.repo.category_exists(body.category_id):
                raise ReferenceNotFoundError(
                    f"Category ID {body.category_id} not found"
                )
            keyword.category_id = body.category_id
        await self.repo.save(keyword)
        kw, count = await self.repo.fetch_one_with_stats(keyword.id)
        return self._build_detail(kw, count)

    async def delete_keyword(self, keyword_id: int) -> None:
        keyword = await self.repo.get_by_id(keyword_id)
        if not keyword:
            raise NotFoundError("Keyword not found")
        await self.repo.delete(keyword)
