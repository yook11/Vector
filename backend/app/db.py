from app.config import settings
from app.db_ssl import create_app_engine
from app.models.base import Base

# FastAPI app 専用 engine。最大 20 connection に制限 (pool_size + max_overflow)。
# resilience (pre_ping / recycle / 飽和時の timeout fail-fast) は
# create_app_engine の既定で全 engine に付与される。ここは sizing のみ明示。
# SSL (Neon verify-full) も同 factory が接続文字列の sslmode から導く。
engine = create_app_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=10,
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
