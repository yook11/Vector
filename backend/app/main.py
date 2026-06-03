from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import logfire
import structlog
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.config import settings
from app.db import (
    API_POOL_MAX_OVERFLOW,
    API_POOL_SIZE,
    API_SERVICE_NAME,
    engine,
)
from app.db_ssl import DEFAULT_POOL_RECYCLE, DEFAULT_POOL_TIMEOUT
from app.exception_handlers import (
    duplicate_handler,
    invalid_query_handler,
    not_found_handler,
)
from app.exceptions import DuplicateError, InvalidQueryError, NotFoundError
from app.insights.briefing.router.briefing import router as briefing_router
from app.insights.trend_discovery.router.weekly_trends import (
    router as weekly_trends_router,
)
from app.logfire_db_pool import log_pool_initialized, register_pool_metrics
from app.logfire_setup import setup_logfire
from app.routers import (
    admin,
    articles,
    categories,
    watchlist,
)

logger = structlog.get_logger(__name__)


def _sanitize_validation_errors(
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pydantic v2 ``ValidationError.errors()`` から rejected input を落とす。

    各 error の ``input`` は送信値そのもの (例: 長すぎる body field で長さ違反
    すれば入力文字列がそのまま入る)、``ctx`` は型検査の文脈値、``url`` は
    pydantic docs URL。これらは PII / 事業データを含みうるため span attribute
    に残すのは ``type`` / ``loc`` / ``msg`` (= どこで何を期待していたか) のみ。
    """
    return [
        {"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")}
        for e in errors
    ]


def _drop_endpoint_args_on_success(
    _request: Request | WebSocket,
    attributes: dict[str, Any],
) -> dict[str, Any] | None:
    """Endpoint 引数の log message 化を抑制 (PII 抑制)。

    成功時は ``None`` を返して log message を作らず span だけ残す
    (= 「成功は沈黙、失敗だけ説明する」)。validation error 時は ``errors`` を
    sanitize してから返し、``values`` (parsed body / query) と各 error の
    ``input`` (送信値) は捨てる。article body などが Logfire dashboard に
    焼かれないよう、失敗の場所だけを見せて入力値は隠す。
    """
    errors = attributes.get("errors")
    if errors:
        return {"errors": _sanitize_validation_errors(errors)}
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # 起動時: 可観測性 (Logfire + structlog 集約) を初期化する。
    # token 未設定の dev/CI/test では完全 no-op (外部送信なし) で安全。
    # タスクワーカーは別の Docker サービス (worker/scheduler) で動作するため、
    # 各 worker プロセスは brokers.py の WORKER_STARTUP で同じ bootstrap を呼ぶ。
    setup_logfire(API_SERVICE_NAME)
    # FastAPI request / SQLAlchemy query の auto-instrument を bootstrap 直後に
    # 1 度だけ走らせる。setup_logfire 内の logfire.configure() が OTel provider
    # を立てたあとに hook する順序契約 (configure 前に呼ぶと patch は走るが span
    # の送り先が proxy provider のまま固定される)。
    #
    # kwargs は source default と一致するが、明示で「PII / body を span に乗せ
    # ない」契約を固定する。特に ``record_send_receive=False`` は ASGI
    # send/receive span 経由で body が乗る経路を塞ぐため明示する。
    logfire.instrument_fastapi(
        app,
        request_attributes_mapper=_drop_endpoint_args_on_success,
        capture_headers=False,
        record_send_receive=False,
        extra_spans=False,
    )
    # logfire は AsyncEngine をネイティブで受ける。
    # 1 query = 1 span として bind param と SQL を span attribute に乗せる。
    # asyncpg instrumentor は意図的に入れない (ORM 層 + driver 層で二重 span
    # を出さないため)。
    logfire.instrument_sqlalchemy(engine=engine)
    log_pool_initialized(
        service_name=API_SERVICE_NAME,
        pool_size=API_POOL_SIZE,
        max_overflow=API_POOL_MAX_OVERFLOW,
        pool_recycle=DEFAULT_POOL_RECYCLE,
        pool_timeout=DEFAULT_POOL_TIMEOUT,
    )
    register_pool_metrics(
        engine, pool_size=API_POOL_SIZE, max_overflow=API_POOL_MAX_OVERFLOW
    )
    yield
    # 終了処理
    await engine.dispose()


# production では Swagger UI / ReDoc / openapi.json を無効化して攻撃面を削減する。
# development では /docs 等を閲覧可能にする。
_docs_enabled = settings.env == "development"

# responses: FastAPI が UTF-8 不正 body 等の malformed request に対して内部生成する
# HTTPException(400, "There was an error parsing the body") を OpenAPI に default
# 宣言する。proxy / middleware 由来の 400 も any endpoint で発生しうるため app level
# に置く。
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


# --- セキュリティヘッダ ミドルウェア ---
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

# --- CORS ミドルウェア ---
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

# ルーター登録
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
# (hey-api SDK) が path 由来の長大な関数名を出力する。Vector では関数名が
# アプリ内でユニークなので、route.name (= 関数名)
# をそのまま operation_id とする FastAPI 標準パターンを採用する。
for _route in app.routes:
    if isinstance(_route, APIRoute):
        _route.operation_id = _route.name
