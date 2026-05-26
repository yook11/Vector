from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.config import settings
from app.db import engine
from app.exception_handlers import (
    duplicate_handler,
    invalid_query_handler,
    not_found_handler,
    search_error_handler,
    search_quota_exceeded_handler,
)
from app.exceptions import DuplicateError, InvalidQueryError, NotFoundError
from app.insights.briefing.router.briefing import router as briefing_router
from app.insights.snapshot.router.weekly_trends import router as weekly_trends_router
from app.logfire_setup import setup_logfire
from app.routers import (
    admin,
    articles,
    categories,
    watchlist,
)
from app.search.errors import SearchError
from app.search.quota import SearchQuotaExceededError
from app.search.router import router as search_router

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # 起動時: 可観測性 (Logfire + structlog 集約) を初期化する。
    # token 未設定の dev/CI/test では完全 no-op (外部送信なし) で安全。
    # タスクワーカーは別の Docker サービス (worker/scheduler) で動作するため、
    # 各 worker プロセスは brokers.py の WORKER_STARTUP で同じ bootstrap を呼ぶ。
    setup_logfire("vector-api")
    yield
    # 終了処理
    await engine.dispose()


# production では Swagger UI / ReDoc / openapi.json を無効化して攻撃面を削減
# (red-team S-EXFIL-1 / C3 amplifier 防御)。/api/v1/admin/* の schema を含む
# 全 endpoint 構造の偵察経路を物理的に閉じる。development では従来通り /docs
# 等で閲覧可能。
_docs_enabled = settings.env == "development"

# responses: FastAPI が UTF-8 不正 body 等の malformed request に対して内部生成する
# HTTPException(400, "There was an error parsing the body") を OpenAPI に default
# 宣言する。proxy / middleware 由来の 400 も any endpoint で発生しうるため app level
# に置く (Schemathesis status_code_conformance finding 対応 / PR-C1a')。
app = FastAPI(
    title="Vector API",
    description="テックニュースの収集と AI 分析プラットフォーム",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    responses={400: {"description": "Bad request"}},
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
    allow_headers=["Authorization", "Content-Type"],
)

# 例外ハンドラ
app.add_exception_handler(NotFoundError, not_found_handler)
app.add_exception_handler(DuplicateError, duplicate_handler)
app.add_exception_handler(InvalidQueryError, invalid_query_handler)

app.add_exception_handler(SearchError, search_error_handler)
app.add_exception_handler(SearchQuotaExceededError, search_quota_exceeded_handler)

# ルーター登録
# NOTE: /articles/search を /articles/{article_id} より先にマッチさせるため、
# semantic_search を articles より先に登録する必要がある。
app.include_router(search_router)
app.include_router(articles.router)
app.include_router(categories.router)
app.include_router(watchlist.router)
app.include_router(weekly_trends_router)
app.include_router(briefing_router)
app.include_router(admin.admin_router)


@app.get("/api/v1/health")
async def health_check() -> dict:
    db_connected = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            db_connected = True
    except Exception as exc:
        logger.warning(
            "health_check_db_unreachable",
            error_type=type(exc).__name__,
        )

    return {
        "status": "ok",
        "version": "0.1.0",
        "dbConnected": db_connected,
    }


# operation_id を route handler の関数名に揃える。
# 指定しない場合 FastAPI 既定で `<fn>_<path>_<method>` 形になり、フロントの型生成
# (hey-api SDK) が `searchArticlesApiV1ArticlesSearchGet` のような長大な関数名を
# 出力する。Vector では関数名がアプリ内でユニークなので、route.name (= 関数名)
# をそのまま operation_id とする FastAPI 標準パターンを採用する。
for _route in app.routes:
    if isinstance(_route, APIRoute):
        _route.operation_id = _route.name
