"""ドメイン例外に対するグローバル例外ハンドラ。

各ハンドラはドメイン例外を対応する HTTP レスポンスに変換する。
main.py の ``app.add_exception_handler()`` で登録する。
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette import status

from app.exceptions import DuplicateError, InvalidQueryError, NotFoundError
from app.search.errors import SearchError
from app.search.quota import SearchQuotaExceededError


async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": exc.detail},
    )


async def duplicate_handler(_request: Request, exc: DuplicateError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": exc.detail},
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
