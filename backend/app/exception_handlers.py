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

logger = structlog.get_logger(__name__)

# NotFoundError / DuplicateError の detail は公開レスポンスに出るため、
# allowlist 形式だけを通し、内部 ID や DB 構造 hint の混入を防ぐ。
_NOT_FOUND_DETAIL_RE = re.compile(r"^[A-Z][A-Za-z ]{1,40} not found$")
_DUPLICATE_DETAIL_RE = re.compile(r"^[A-Z][A-Za-z ]{1,40} already exists$")


def _safe_detail(raw_detail: str, allow_re: re.Pattern[str], fallback: str) -> str:
    """allowlist にマッチする detail だけを通し、それ以外は generic に丸める。

    raw 文字列は log に焼かず、長さと sha256 prefix だけを記録する。
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
