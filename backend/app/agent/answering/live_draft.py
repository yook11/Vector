"""回答下書きのライブ表示ライフサイクルを管理する。"""

from __future__ import annotations

from types import TracebackType
from typing import Literal, Self

from app.agent.answering.visible_text import AnswerVisibleTextFilter
from app.agent.contract import AnswerDeltaReporter

__all__ = ["LiveAnswerDraftSession"]


class LiveAnswerDraftSession:
    """1 generationの表示用回答下書きを成功または中断として閉じる。"""

    def __init__(
        self,
        *,
        generation: int,
        delta_reporter: AnswerDeltaReporter,
    ) -> None:
        self._generation = generation
        self._delta = delta_reporter
        self._visible_filter = AnswerVisibleTextFilter()
        self._closed = False

    async def __aenter__(self) -> Self:
        self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if not self._closed:
            await self.abort()
        return False

    async def append(self, text: str) -> None:
        self._ensure_open()
        visible = self._visible_filter.append(text)
        if visible:
            await self._delta.append(generation=self._generation, text=visible)

    async def commit(self) -> None:
        self._ensure_open()
        visible_tail = self._visible_filter.finish()
        if visible_tail:
            await self._delta.append(
                generation=self._generation,
                text=visible_tail,
            )
        await self._delta.finish(generation=self._generation)
        self._closed = True

    async def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._delta.abort(generation=self._generation)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("closed live answer draft session cannot be reused")
