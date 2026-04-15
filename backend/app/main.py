from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.analysis.errors import AnalysisDomainError
from app.config import settings
from app.db import engine
from app.exception_handlers import (
    duplicate_handler,
    embedding_error_handler,
    not_found_handler,
)
from app.exceptions import DuplicateError, NotFoundError
from app.routers import (
    admin,
    articles,
    categories,
    semantic_search,
    watchlist,
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


# --- セキュリティヘッダ ミドルウェア (4.16.3 / 4.16.9) ---
# X-Content-Type-Options: nosniff
#   ブラウザの MIME スニッフィングを無効化する。
#   Content-Type が application/json でも、ブラウザが中身を見て HTML と推測し
#   レンダリングしてしまう問題（JSON 直接閲覧 XSS）を防止する。
# X-Frame-Options: DENY
#   iframe 内での表示を全面禁止し、クリックジャッキング攻撃を防止する。
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """全レスポンスにセキュリティヘッダを付与するミドルウェア。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


# Starlette/FastAPI のミドルウェア登録順序:
# 後に登録したミドルウェアが外側にラップされるため、SecurityHeaders は
# CORS ヘッダが設定された後の全レスポンスに適用される。
app.add_middleware(SecurityHeadersMiddleware)

# --- CORS ミドルウェア (4.16.8) ---
# 最小権限の原則: ワイルドカード ("*") ではなく、実際に使用するメソッドと
# ヘッダのみを許可する。
# allow_origins: フロントエンドのオリジンのみ許可
# allow_methods: API が受け付ける HTTP メソッドのみ
# allow_headers: Authorization (JWT Bearer) と Content-Type (JSON body) のみ
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

# Exception handlers
app.add_exception_handler(NotFoundError, not_found_handler)
app.add_exception_handler(DuplicateError, duplicate_handler)

app.add_exception_handler(AnalysisDomainError, embedding_error_handler)

# Register routers
# NOTE: semantic_search must be registered before articles
# so that /articles/search is matched before /articles/{article_id}
app.include_router(semantic_search.router)
app.include_router(articles.router)
app.include_router(categories.router)
app.include_router(watchlist.router)
app.include_router(admin.admin_router)


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
