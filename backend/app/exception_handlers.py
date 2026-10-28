"""ドメイン例外に対するグローバル例外ハンドラ。

各ハンドラはドメイン例外を対応する HTTP レスポンスに変換する。
main.py の ``app.add_exception_handler()`` で登録する。
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette import status

from app.exceptions import InvalidQueryError, NotFoundError


async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": exc.detail},
    )


async def invalid_query_handler(
    _request: Request, exc: InvalidQueryError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.detail},
    )
