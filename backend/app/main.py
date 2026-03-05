from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.routers import (
    auth,
    categories,
    keyword_categories,
    keywords,
    me,
    news,
    news_sources,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup: task worker runs in a separate Docker service (worker/scheduler).
    # Add future startup logic here if needed (e.g. cache warming, connection checks).
    yield
    # Shutdown
    await engine.dispose()


app = FastAPI(
    title="Vector API",
    description="Tech news aggregation & AI analysis platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth.router)
app.include_router(categories.router)
app.include_router(keyword_categories.router)
app.include_router(keywords.router)
app.include_router(me.router)
app.include_router(news.router)
app.include_router(news_sources.router)


@app.get("/api/v1/health")
async def health_check() -> dict:
    db_connected = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            db_connected = True
    except Exception:
        pass

    return {
        "status": "ok",
        "version": "0.1.0",
        "dbConnected": db_connected,
    }
