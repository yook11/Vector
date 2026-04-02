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

    async def list_sources(self) -> NewsSourceDetailList:
        sources = await self.repo.get_all()
        count = await self.repo.get_count()
        return NewsSourceDetailList(
            items=[NewsSourceDetail.model_validate(s) for s in sources],
            total=count,
        )

    async def get_source(self, source: NewsSource) -> NewsSourceDetail:
        return NewsSourceDetail.model_validate(source)

    async def create_source(self, body: NewsSourceCreate) -> NewsSourceDetail:
        source = NewsSource(
            name=body.name,
            source_type=body.source_type,
            site_url=body.site_url,
            endpoint_url=body.endpoint_url,
        )
        source = await self.repo.create(source)
        return NewsSourceDetail.model_validate(source)

    async def delete_source(self, source: NewsSource) -> None:
        await self.repo.delete(source)

    async def toggle_source(self, source: NewsSource) -> NewsSourceDetail:
        source.is_active = not source.is_active
        source = await self.repo.save(source)
        return NewsSourceDetail.model_validate(source)
