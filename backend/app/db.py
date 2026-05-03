from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from app.config import settings

# Pool 設定明示 (red-team C6 / F18 対策)。
#
# SQLAlchemy default (pool_size=5 + max_overflow=10 + pool_timeout=30s) は
# 16 並列を超えた瞬間に 17 番目以降を 30 秒ハングさせ、後続の正規 user request
# も連鎖的に hang する増幅源になる。frontend pg.Pool (lib/auth/auth.ts:
# max=20 + connectionTimeoutMillis=5000) と思想を揃え、5 秒で fail-fast →
# Pool 飽和は 5xx を即返して上位の rate-limit (proxy.ts) で吸収させる。
#
# pool_size=10 + max_overflow=10 = 同時 20 connection。worker / scheduler は
# app/brokers.py で独立 engine を持つので、本 engine は backend FastAPI app
# 専用 (Pool 隔離済)。pool_pre_ping で stale connection を検出、
# pool_recycle=3600 で idle connection を 1 時間で drop し PostgreSQL 側の
# idle-in-transaction timeout 等に引っかからないようにする。
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
        await conn.run_sync(SQLModel.metadata.create_all)
