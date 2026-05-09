"""ドメイン例外に対するグローバル例外ハンドラ。

各ハンドラはドメイン例外を対応する HTTP レスポンスに変換する。
main.py の ``app.add_exception_handler()`` で登録する。
"""

import hashlib
import re

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette import status

from app.exceptions import DuplicateError, InvalidQueryError, NotFoundError
from app.search.errors import SearchError
from app.search.quota import SearchQuotaExceededError

logger = structlog.get_logger(__name__)

# red-team chain θ-1: NotFoundError / DuplicateError の detail に内部 ID /
# source 名 / DB 構造 hint が紛れ込む regression を defense-in-depth で閉鎖する。
# 許可 form は `<Entity> not found` / `<Entity> already exists` のみ。
_NOT_FOUND_DETAIL_RE = re.compile(r"^[A-Z][A-Za-z ]{1,40} not found$")
_DUPLICATE_DETAIL_RE = re.compile(r"^[A-Z][A-Za-z ]{1,40} already exists$")


def _safe_detail(raw_detail: str, allow_re: re.Pattern[str], fallback: str) -> str:
    """allowlist にマッチする detail のみ pass、それ以外は generic に丸める。

    log 出力では raw 文字列を焼かず、長さ + sha256 prefix のみ記録する
    (将来 ``f"Article {user_input} not found"`` 経路が紛れた場合の log
    injection 二次経路を防ぐ)。
    """
    if allow_re.fullmatch(raw_detail):
        return raw_detail
    digest = hashlib.sha256(raw_detail.encode("utf-8")).hexdigest()[:16]
    logger.warning(
        "exception_detail_blocked_by_allowlist",
        raw_detail_length=len(raw_detail),
        raw_detail_sha256_prefix=digest,
        fallback=fallback,
    )
    return fallback


async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
    detail = _safe_detail(exc.detail, _NOT_FOUND_DETAIL_RE, "Resource not found")
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": detail},
    )


async def duplicate_handler(_request: Request, exc: DuplicateError) -> JSONResponse:
    detail = _safe_detail(exc.detail, _DUPLICATE_DETAIL_RE, "Resource already exists")
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": detail},
    )


async def invalid_query_handler(
    _request: Request, exc: InvalidQueryError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.detail},
    )


async def search_error_handler(_request: Request, _exc: SearchError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Search embedding generation failed. Please try again."},
    )


async def search_quota_exceeded_handler(
    _request: Request, _exc: SearchQuotaExceededError
) -> JSONResponse:
    """red-team C1 対策: per-user 日次 search quota 超過を 429 にマップする。"""
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "detail": "Daily search quota exceeded. Please retry after 24 hours.",
        },
    )
