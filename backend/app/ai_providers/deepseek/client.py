"""利用範囲内でDeepSeekクライアントを生成し、所有する資源を閉じる。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import structlog
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.ai_providers.deepseek.error_translator import DeepSeekStateReason
from app.ai_providers.deepseek.settings import DeepSeekConnectionSettings
from app.ai_providers.errors import AIProviderConfigurationError
from app.http.external import make_external_async_client

logger = structlog.get_logger(__name__)


async def _close(resource: str, close: Callable[[], Awaitable[None]]) -> None:
    try:
        await close()
    except Exception as exc:
        try:
            logger.warning(
                "deepseek_client_cleanup_failed",
                resource=resource,
                error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
        except Exception:  # noqa: S110
            # 終了診断の障害で利用結果を変更しない。
            pass


@asynccontextmanager
async def open_deepseek_client(
    *, api_key: SecretStr, base_url: str, settings: DeepSeekConnectionSettings
) -> AsyncIterator[AsyncOpenAI]:
    """SDKとHTTPクライアントを所有する。"""
    if not api_key.get_secret_value().strip():
        raise AIProviderConfigurationError(reason=DeepSeekStateReason.NOT_CONFIGURED)
    timeout = httpx.Timeout(
        connect=settings.connect_timeout,
        read=settings.read_timeout,
        write=settings.write_timeout,
        pool=settings.pool_timeout,
    )
    async with AsyncExitStack() as stack:
        http_client = make_external_async_client(
            timeout=timeout, retries=0, follow_redirects=False
        )

        async def close_http() -> None:
            # SDKの生成・終了が失敗しても、未解放のHTTP資源を閉じる。
            if not http_client.is_closed:
                await http_client.aclose()

        stack.push_async_callback(_close, "http", close_http)
        client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            http_client=http_client,
            timeout=timeout,
            max_retries=0,
        )
        stack.push_async_callback(_close, "sdk", client.close)
        yield client
