from app.config import settings
from app.db_ssl import create_app_engine
from app.models.base import Base

API_SERVICE_NAME = "vector-api"
API_POOL_SIZE = 10
API_POOL_MAX_OVERFLOW = 10

# FastAPI app 専用 engine。最大同時接続は pool_size + max_overflow = 20。
# SSL と resilience は create_app_engine の既定に任せる。
engine = create_app_engine(
    settings.database_url,
    application_name=API_SERVICE_NAME,
    echo=False,
    pool_size=API_POOL_SIZE,
    max_overflow=API_POOL_MAX_OVERFLOW,
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
