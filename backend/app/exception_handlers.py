"""Global exception handlers for domain exceptions.

Each handler converts a domain exception to an appropriate HTTP response.
Registered in main.py via app.add_exception_handler().
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette import status

from app.ai.embedding import EmbeddingError
from app.exceptions import DuplicateError, NotFoundError


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


async def embedding_error_handler(
    _request: Request, _exc: EmbeddingError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Search embedding generation failed. Please try again."},
    )
