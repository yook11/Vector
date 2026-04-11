from app.exceptions import NotFoundError
from app.models.news_source import NewsSource
from app.repositories.news_source import NewsSourceRepository
from app.schemas.news_source import (
    NewsSourceCreate,
    NewsSourceDetail,
    NewsSourceDetailList,
)


class NewsSourceService:
    def __init__(self, repo: NewsSourceRepository) -> None:
        self.repo = repo

    async def get_all(self) -> NewsSourceDetailList:
        sources = await self.repo.get_all()
        return NewsSourceDetailList(
            items=[NewsSourceDetail.model_validate(s) for s in sources],
        )

    async def _get_or_raise(self, source_id: int) -> NewsSource:
        source = await self.repo.get_by_id(source_id)
        if source is None:
            raise NotFoundError("News source not found")
        return source

    async def create_source(self, body: NewsSourceCreate) -> NewsSourceDetail:
        source = NewsSource(
            name=body.name,
            source_type=body.source_type,
            site_url=body.site_url,
            endpoint_url=body.endpoint_url,
        )
        await self.repo.create(source)
        return NewsSourceDetail.model_validate(source)

    async def delete_source(self, source_id: int) -> None:
        source = await self._get_or_raise(source_id)
        await self.repo.delete(source)

    async def activate_source(self, source_id: int) -> NewsSourceDetail:
        source = await self._get_or_raise(source_id)
        source.activate()
        return NewsSourceDetail.model_validate(source)

    async def deactivate_source(self, source_id: int) -> NewsSourceDetail:
        source = await self._get_or_raise(source_id)
        source.deactivate()
        return NewsSourceDetail.model_validate(source)
