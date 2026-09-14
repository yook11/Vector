# ruff: noqa: S101
"""記事のHTTP応答を登録順に停止し、テストから再開できるようにする。"""

import asyncio
from collections import deque
from dataclasses import dataclass, field

import httpx


@dataclass
class ResponseGate:
    response: httpx.Response
    requested: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait_requested(self):
        await asyncio.wait_for(self.requested.wait(), 10)

    def release(self):
        self.released.set()


class GatedResponses:
    def __init__(self):
        self._waiting: deque[ResponseGate] = deque()
        self._registered: list[ResponseGate] = []

    async def respond(self, request):
        assert self._waiting, "登録数を超えて記事取得が実行された"
        gate = self._waiting.popleft()
        gate.requested.set()
        await asyncio.wait_for(gate.released.wait(), 15)
        return gate.response

    def register(self, response):
        gate = ResponseGate(response)
        self._waiting.append(gate)
        self._registered.append(gate)
        return gate

    def release_all(self):
        for gate in self._registered:
            gate.release()
