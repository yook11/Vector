"""工程を問わず使える、外部HTTP応答の差し替えとリクエスト記録。"""

from collections.abc import Awaitable, Callable

import httpx2


class StubHttp:
    """外部通信を指定した応答へ置き換え、リクエストを記録する。"""

    def __init__(
        self,
        respond: Callable[[httpx2.Request], Awaitable[httpx2.Response]],
    ):
        self._respond = respond
        self.requests: list[httpx2.Request] = []

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return await self._respond(request)

    def create_client(self, **kwargs) -> httpx2.AsyncClient:  # noqa: TID251
        return httpx2.AsyncClient(  # noqa: TID251
            transport=httpx2.MockTransport(self._handle),
            **kwargs,
        )
