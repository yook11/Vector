from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.models.base import Base

# FastAPI app 専用 engine。最大 20 connection に制限し、pool 飽和は
# 5 秒で fail-fast させる。pre_ping / recycle で stale connection を避ける。
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=10,
    pool_timeout=5,
    pool_pre_ping=True,
    pool_recycle=3600,
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
