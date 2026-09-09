"""利用範囲内でGeminiクライアントを生成し、所有する資源を閉じる。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import structlog
from google import genai
from google.genai.client import AsyncClient
from google.genai.types import HttpOptions, HttpRetryOptions
from pydantic import SecretStr

from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.http.external import make_external_async_client

logger = structlog.get_logger(__name__)


def _record_cleanup_failure(resource: str, exc: Exception) -> None:
    try:
        logger.warning(
            "gemini_client_cleanup_failed",
            resource=resource,
            error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )
    except Exception:  # noqa: S110
        # 終了診断の障害で利用結果を変更しない。
        pass


async def _close_async(resource: str, close: Callable[[], Awaitable[None]]) -> None:
    try:
        await close()
    except Exception as exc:
        _record_cleanup_failure(resource, exc)


def _close_sync(resource: str, close: Callable[[], None]) -> None:
    try:
        close()
    except Exception as exc:
        _record_cleanup_failure(resource, exc)


@asynccontextmanager
async def open_gemini_client(
    *, api_key: SecretStr, settings: GeminiConnectionSettings
) -> AsyncIterator[AsyncClient]:
    """SDKとHTTPクライアントを所有し、呼び出し間では共有しない。"""
    if not api_key.get_secret_value().strip():
        raise ValueError("Gemini API key must not be empty")
    timeout = httpx.Timeout(
        connect=settings.connect_timeout,
        read=settings.read_timeout,
        write=settings.write_timeout,
        pool=settings.pool_timeout,
    )

    async def apply_timeout(request: httpx.Request) -> None:
        # SDKの要求単位の指定より、この接続に宣言された上限を優先する。
        request.extensions["timeout"] = timeout.as_dict()

    async with AsyncExitStack() as stack:
        http_client = make_external_async_client(
            timeout=timeout,
            retries=0,
            follow_redirects=False,
            event_hooks={"request": [apply_timeout]},
        )
        stack.push_async_callback(_close_async, "http", http_client.aclose)
        client = genai.Client(
            api_key=api_key.get_secret_value(),
            enterprise=False,
            http_options=HttpOptions(
                httpx_async_client=http_client,
                retry_options=HttpRetryOptions(attempts=1),
            ),
        )
        stack.callback(_close_sync, "sdk_sync", client.close)
        async_client = client.aio
        stack.push_async_callback(_close_async, "sdk_async", async_client.aclose)
        yield async_client
