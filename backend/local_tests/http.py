"""工程を問わず使える、外部HTTP応答の差し替えとリクエスト記録。"""

from collections.abc import Awaitable, Callable

import httpx


class StubHttp:
    """外部通信を指定した応答へ置き換え、リクエストを記録する。"""

    def __init__(
        self,
        respond: Callable[[httpx.Request], Awaitable[httpx.Response]],
    ):
        self._respond = respond
        self.requests: list[httpx.Request] = []

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._respond(request)

    def create_client(self, **kwargs) -> httpx.AsyncClient:  # noqa: TID251
        return httpx.AsyncClient(  # noqa: TID251
            transport=httpx.MockTransport(self._handle),
            **kwargs,
        )
